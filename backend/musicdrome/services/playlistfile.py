"""Import ``.m3u`` / ``.m3u8`` playlist files sitting in the library.

Downloaders write a playlist file next to the audio they fetch — Downtify puts
one in ``<downloads>/Playlists/<name>.m3u`` with the track paths written
*relative to the m3u itself*, spotDL and others do something similar. This
module finds those files and turns them into real playlists, the way Navidrome
does on its scan.

The file on disk stays the source of truth: its mtime drives a re-import, and
deleting it deletes the playlist. That reverses the moment somebody edits the
track list from the web UI or a Subsonic client — the playlist is then theirs,
and nothing here touches it again.

Resolution of an entry to a library track goes down a ladder, stopping at the
first hit:

1. the path as written, resolved against the playlist file's own folder
2. the same, case-insensitively — for libraries that came off a Windows share
3. the trailing path segments, which survives the library being mounted at a
   different root inside the container than it had when the file was written
4. the ``#EXTINF`` artist/title, for a playlist written against a copy of the
   library that was organised differently

Steps 3 and 4 only accept an unambiguous match: if two tracks would satisfy
them the entry is treated as missing rather than guessed at.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import session_scope, utcnow
from ..models import Playlist, PlaylistTrack, Track, User
from .playlists import recalculate, replace_tracks
from .scanner import is_ignored

log = logging.getLogger(__name__)

# One import pass at a time — the scanner, the watcher, the scheduler and a
# manual trigger can all ask at once, and they would otherwise fight over the
# same rows.
_import_lock = threading.Lock()
_last_run: dict[str, object] = {
    "at": None,
    "files": 0,
    "created": 0,
    "updated": 0,
    "deleted": 0,
    "missing": 0,
    "errors": 0,
}

_EXTINF_PREFIX = "#EXTINF:"
_PLAYLIST_PREFIX = "#PLAYLIST:"
_ARTIST_PREFIX = "#EXTART:"
_LEADING_NUMBER = re.compile(r"^\s*(-?\d+)")
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_REMOTE_SCHEMES = {"http", "https", "ftp", "ftps", "rtsp", "mms"}


# ─── Parsing ───────────────────────────────────────────────────────────────


@dataclass
class M3UEntry:
    """One media line, plus whatever ``#EXTINF`` said about it."""

    target: str
    artist: str = ""
    title: str = ""
    duration: int = 0


@dataclass
class M3UDocument:
    name: str = ""
    entries: list[M3UEntry] = field(default_factory=list)


def _decode(raw: bytes) -> str:
    """Best-effort text decode.

    ``.m3u8`` is UTF-8 by definition and ``.m3u`` is whatever the writing tool
    felt like, which in practice means UTF-8 or a Windows codepage.
    """
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _split_display(display: str) -> tuple[str, str]:
    """``"Artist - Title"`` → ``("Artist", "Title")``, tolerating neither."""
    for separator in (" - ", " – ", " — "):
        artist, found, title = display.partition(separator)
        if found and artist.strip() and title.strip():
            return artist.strip(), title.strip()
    return "", display.strip()


def parse_m3u_text(text: str, *, name: str = "") -> M3UDocument:
    document = M3UDocument(name=name)
    pending_artist = ""
    pending_title = ""
    pending_duration = 0

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        if line.startswith("#"):
            upper = line.upper()
            if upper.startswith(_EXTINF_PREFIX):
                payload = line[len(_EXTINF_PREFIX):]
                # "123,Artist - Title", or "-1 tvg-id=\"x\",Title" from the
                # IPTV dialect — everything before the first comma is metadata.
                head, _, display = payload.partition(",")
                match = _LEADING_NUMBER.match(head)
                pending_duration = max(0, int(match.group(1))) if match else 0
                pending_artist, pending_title = _split_display(display.strip())
            elif upper.startswith(_PLAYLIST_PREFIX):
                declared = line[len(_PLAYLIST_PREFIX):].strip()
                if declared:
                    document.name = declared
            elif upper.startswith(_ARTIST_PREFIX):
                pending_artist = line[len(_ARTIST_PREFIX):].strip()
            continue

        document.entries.append(
            M3UEntry(
                target=line,
                artist=pending_artist,
                title=pending_title,
                duration=pending_duration,
            )
        )
        pending_artist = pending_title = ""
        pending_duration = 0

    return document


def parse_m3u_bytes(raw: bytes, *, name: str = "") -> M3UDocument:
    return parse_m3u_text(_decode(raw), name=name)


def parse_m3u(path: Path) -> M3UDocument:
    return parse_m3u_bytes(path.read_bytes(), name=path.stem)


# ─── Track resolution ──────────────────────────────────────────────────────


def _normalise(value: str) -> str:
    """Collapse ``..``/``.`` segments and settle on forward slashes."""
    return os.path.normpath(value).replace("\\", "/")


def _clean_target(raw: str) -> str:
    """Turn a media line into something ``Path`` can work with."""
    target = raw.strip().strip('"').strip("'")
    if not target:
        return ""

    if target.lower().startswith("file://"):
        parsed = urlparse(target)
        target = unquote(parsed.path)
        # file:///C:/Music/x.mp3 parses with a leading slash before the drive
        if re.match(r"^/[A-Za-z]:", target):
            target = target[1:]
    elif _PERCENT_ESCAPE.search(target) and " " not in target:
        # Percent escapes only mean something when the writer used them; a path
        # with real spaces was plainly never escaped.
        target = unquote(target)

    if "\\" in target and "/" not in target:
        target = target.replace("\\", "/")  # written on Windows
    return target


def _is_remote(target: str) -> bool:
    scheme = urlparse(target).scheme.lower()
    return scheme in _REMOTE_SCHEMES


@dataclass
class TrackIndex:
    """Lookup tables over the library, built once per import pass.

    ``None`` as a value marks an ambiguous key — two or more tracks answer to
    it, so it must not be used to resolve anything.
    """

    by_path: dict[str, int] = field(default_factory=dict)
    by_ci_path: dict[str, int | None] = field(default_factory=dict)
    by_tail: dict[str, int | None] = field(default_factory=dict)
    by_pair: dict[str, int | None] = field(default_factory=dict)
    by_title: dict[str, int | None] = field(default_factory=dict)

    def _add(self, mapping: dict[str, int | None], key: str, track_id: int) -> None:
        if not key:
            return
        if key in mapping and mapping[key] != track_id:
            mapping[key] = None  # ambiguous from here on
        else:
            mapping.setdefault(key, track_id)

    def add(self, track_id: int, path: str, title: str, artist: str) -> None:
        normalised = _normalise(path)
        self.by_path[normalised] = track_id
        self._add(self.by_ci_path, normalised.lower(), track_id)

        parts = PurePosixPath(normalised).parts
        for depth in (1, 2, 3):
            if len(parts) < depth:
                break
            tail = "/".join(parts[-depth:]).lower()
            self._add(self.by_tail, tail, track_id)
            stem, extension = os.path.splitext(tail)
            if extension:
                self._add(self.by_tail, stem, track_id)

        if artist and title:
            self._add(self.by_pair, f"{artist.lower()}\t{title.lower()}", track_id)
        self._add(self.by_title, title.lower(), track_id)


def build_index(db: Session) -> TrackIndex:
    index = TrackIndex()
    for track_id, path, title, artist in db.execute(
        select(Track.id, Track.path, Track.title, Track.artist_name)
    ).all():
        index.add(track_id, path, title or "", artist or "")
    return index


def resolve_entry(entry: M3UEntry, base: Path, index: TrackIndex) -> int | None:
    """Find the library track an entry refers to, or ``None``."""
    target = _clean_target(entry.target)
    if not target or _is_remote(target):
        return None

    candidate_path = Path(target)
    candidates = (
        [candidate_path]
        if candidate_path.is_absolute()
        # Relative to the playlist file first — that is what the writers mean —
        # then to the library root, for files that were written against it.
        else [base / target, settings.music_dir / target]
    )
    for candidate in candidates:
        normalised = _normalise(str(candidate))
        hit = index.by_path.get(normalised) or index.by_ci_path.get(normalised.lower())
        if hit:
            return hit

    parts = [
        part
        for part in PurePosixPath(target.replace("\\", "/")).parts
        if part not in ("/", ".", "..")
    ]
    for depth in (3, 2, 1):
        if len(parts) < depth:
            continue
        tail = "/".join(parts[-depth:]).lower()
        hit = index.by_tail.get(tail)
        if hit:
            return hit
        stem, extension = os.path.splitext(tail)
        if extension:
            hit = index.by_tail.get(stem)
            if hit:
                return hit

    if entry.artist and entry.title:
        hit = index.by_pair.get(f"{entry.artist.lower()}\t{entry.title.lower()}")
        if hit:
            return hit
    if entry.title:
        hit = index.by_title.get(entry.title.lower())
        if hit:
            return hit
    return None


def resolve_document(
    document: M3UDocument, base: Path, index: TrackIndex
) -> tuple[list[int], list[M3UEntry]]:
    """Split a parsed document into resolved track ids and unmatched entries."""
    track_ids: list[int] = []
    missing: list[M3UEntry] = []
    for entry in document.entries:
        track_id = resolve_entry(entry, base, index)
        if track_id is None:
            missing.append(entry)
        else:
            track_ids.append(track_id)
    return track_ids, missing


# ─── Discovery ─────────────────────────────────────────────────────────────


def discover(roots: list[Path] | None = None) -> list[Path]:
    """Every playlist file under the configured roots."""
    extensions = settings.playlist_extensions
    found: list[Path] = []
    seen: set[str] = set()

    for root in roots or settings.playlist_import_roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            if path.suffix.lower().lstrip(".") not in extensions:
                continue
            relative = path.relative_to(root) if path.is_relative_to(root) else path
            if is_ignored(relative):
                continue
            key = str(path)
            if key not in seen:
                seen.add(key)
                found.append(path)
    return found


def import_owner(db: Session) -> User | None:
    """The account imported playlists belong to.

    Navidrome hands them to the first admin; ``PLAYLIST_IMPORT_OWNER`` overrides
    that when they should live somewhere else.
    """
    if settings.playlist_import_owner:
        user = db.scalar(
            select(User).where(User.username == settings.playlist_import_owner)
        )
        if user is not None:
            return user
        log.warning(
            "PLAYLIST_IMPORT_OWNER='%s' matches no account — using the first admin",
            settings.playlist_import_owner,
        )

    admin = db.scalar(
        select(User)
        .where(User.is_admin.is_(True), User.is_active.is_(True))
        .order_by(User.id)
    )
    return admin or db.scalar(select(User).order_by(User.id))


# ─── Import ────────────────────────────────────────────────────────────────


@dataclass
class ImportResult:
    path: str
    name: str = ""
    playlist_id: int | None = None
    # created | updated | unchanged | detached | failed
    action: str = "unchanged"
    matched: int = 0
    missing: int = 0
    error: str = ""


def import_file(
    db: Session,
    path: Path,
    index: TrackIndex,
    owner: User,
    *,
    force: bool = False,
) -> ImportResult:
    """Create or re-sync the playlist behind one ``.m3u`` file."""
    result = ImportResult(path=str(path), name=path.stem)

    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        result.action = "failed"
        result.error = str(exc)
        return result

    existing = db.scalar(select(Playlist).where(Playlist.import_path == str(path)))
    if existing is not None:
        result.playlist_id = existing.id
        result.name = existing.name
        if not existing.sync:
            # Somebody edited it here; the file no longer drives it.
            result.action = "detached"
            return result
        unchanged = abs(existing.import_mtime - mtime) < 1.0
        # A playlist still carrying unmatched entries is re-read even when the
        # file has not moved — the tracks it wanted may have landed since.
        if unchanged and not existing.import_missing and not force:
            result.matched = existing.song_count
            return result

    try:
        document = parse_m3u(path)
    except OSError as exc:
        result.action = "failed"
        result.error = str(exc)
        return result

    track_ids, missing = resolve_document(document, path.parent, index)
    result.matched = len(track_ids)
    result.missing = len(missing)
    for entry in missing[:5]:
        log.debug("no library track for '%s' in %s", entry.target, path.name)

    playlist = existing
    if playlist is None:
        playlist = Playlist(
            name=document.name or path.stem,
            comment=f"Imported from {path.name}",
            owner_id=owner.id,
            public=settings.playlist_import_public,
            is_imported=True,
            import_path=str(path),
            sync=True,
        )
        db.add(playlist)
        db.flush()
        result.action = "created"
        result.playlist_id = playlist.id
        result.name = playlist.name
    else:
        # The name is only taken from the file on first import, so renaming the
        # playlist in the UI is not undone by the next pass.
        current = [
            entry.track_id
            for entry in db.scalars(
                select(PlaylistTrack)
                .where(PlaylistTrack.playlist_id == playlist.id)
                .order_by(PlaylistTrack.position)
            ).all()
        ]
        if current == track_ids and playlist.import_missing == len(missing):
            playlist.import_mtime = mtime  # nothing to write but the timestamp
            db.add(playlist)
            return result
        result.action = "updated"

    replace_tracks(db, playlist, track_ids)
    recalculate(db, playlist)
    playlist.import_mtime = mtime
    playlist.import_missing = len(missing)
    playlist.imported_at = utcnow()
    db.add(playlist)
    return result


def _prune(db: Session, seen: set[str], roots: list[Path]) -> int:
    """Delete synced playlists whose file has gone from a root we can see.

    Guarded on the root still being mounted: a disappeared volume must look
    like a disappeared volume, not like the user deleting every playlist.
    """
    removed = 0
    candidates = db.scalars(
        select(Playlist).where(
            Playlist.is_imported.is_(True),
            Playlist.sync.is_(True),
            Playlist.import_path.isnot(None),
        )
    ).all()

    for playlist in candidates:
        source = str(playlist.import_path)
        if source in seen or Path(source).exists():
            continue
        if not any(Path(source).is_relative_to(root) for root in roots):
            continue  # its root is not mounted right now
        log.info("playlist file %s is gone — removing '%s'", source, playlist.name)
        db.delete(playlist)
        removed += 1
    return removed


def import_all(*, force: bool = False, prune: bool | None = None) -> dict[str, object]:
    """Walk the configured roots and reconcile every playlist file found."""
    if not _import_lock.acquire(blocking=False):
        log.debug("playlist import already running, skipping")
        return {**_last_run, "skipped": True}

    stats = {
        "files": 0,
        "created": 0,
        "updated": 0,
        "deleted": 0,
        "missing": 0,
        "errors": 0,
    }
    try:
        roots = [root for root in settings.playlist_import_roots if root.is_dir()]
        if not roots:
            log.warning(
                "no playlist import root exists (%s) — nothing to import",
                ", ".join(str(r) for r in settings.playlist_import_roots),
            )
            return {**stats, "at": utcnow()}

        files = discover(roots)
        stats["files"] = len(files)

        with session_scope() as db:
            owner = import_owner(db)
            if owner is None:
                log.warning("no user account yet — deferring playlist import")
                return {**stats, "at": utcnow()}

            index = build_index(db)
            seen: set[str] = set()

            for path in files:
                seen.add(str(path))
                try:
                    result = import_file(db, path, index, owner, force=force)
                except Exception:
                    log.exception("failed to import playlist %s", path)
                    stats["errors"] += 1
                    db.rollback()
                    continue

                if result.action == "created":
                    stats["created"] += 1
                elif result.action == "updated":
                    stats["updated"] += 1
                elif result.action == "failed":
                    stats["errors"] += 1
                    log.warning("could not read %s: %s", path, result.error)
                stats["missing"] += result.missing

            should_prune = settings.playlist_import_prune if prune is None else prune
            if should_prune:
                stats["deleted"] = _prune(db, seen, roots)

            db.commit()

        if stats["created"] or stats["updated"] or stats["deleted"]:
            log.info(
                "playlist import: %d created, %d updated, %d removed "
                "(%d files, %d entries not in the library)",
                stats["created"], stats["updated"], stats["deleted"],
                stats["files"], stats["missing"],
            )
    finally:
        _import_lock.release()

    _last_run.update({**stats, "at": utcnow()})
    return dict(_last_run)


def last_run() -> dict[str, object]:
    return dict(_last_run)


# ─── Export ────────────────────────────────────────────────────────────────


def export_m3u(db: Session, playlist: Playlist) -> str:
    """Render a playlist as an extended M3U8 document.

    Paths are written relative to the music directory when the track lives
    under it, so the file survives the library being mounted somewhere else.
    """
    lines = ["#EXTM3U", f"#PLAYLIST:{playlist.name}"]

    entries = db.scalars(
        select(PlaylistTrack)
        .where(PlaylistTrack.playlist_id == playlist.id)
        .order_by(PlaylistTrack.position)
    ).all()

    for entry in entries:
        track = entry.track
        if track is None:
            continue
        display = f"{track.artist_name} - {track.title}" if track.artist_name else track.title
        lines.append(f"#EXTINF:{track.duration},{display}")

        path = Path(track.path)
        if path.is_relative_to(settings.music_dir):
            lines.append(str(path.relative_to(settings.music_dir)))
        else:
            lines.append(str(path))

    return "\n".join(lines) + "\n"
