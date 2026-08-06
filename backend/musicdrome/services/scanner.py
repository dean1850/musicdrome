"""Library scanner.

Walks ``MUSIC_DIR``, reads tags, and reconciles the database against what is
actually on disk. Unchanged files are skipped by comparing mtime, so a rescan of
a large library costs a stat per file rather than a full tag parse.

Two entry points:

* :func:`scan_library` — the full reconcile, run on startup and on a timer.
* :func:`scan_paths` — targeted rescan of specific files or folders, used by the
  filesystem watcher and after an import lands new files.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..db import session_scope, utcnow
from ..models import Album, Artist, Genre, ScanRun, Track
from .tags import TrackTags, extract_embedded_art, read_tags

log = logging.getLogger(__name__)

# One scan at a time. The watcher, the scheduler and a manual trigger can all
# ask concurrently; the loser is told a scan is already running.
_scan_lock = threading.Lock()
_scan_state: dict[str, object] = {"scanning": False, "count": 0, "total": 0, "run_id": None}


@dataclass
class ScanResult:
    seen: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "seen": self.seen,
            "added": self.added,
            "updated": self.updated,
            "removed": self.removed,
            "errors": self.errors,
        }


def scan_state() -> dict[str, object]:
    return dict(_scan_state)


def is_scanning() -> bool:
    return bool(_scan_state.get("scanning"))


# ─── Filesystem walking ────────────────────────────────────────────────────


def is_ignored(path: Path) -> bool:
    parts = set(path.parts)
    for pattern in settings.ignore_patterns:
        if pattern in parts:
            return True
        if any(fnmatch.fnmatch(part, pattern) for part in path.parts):
            return True
    return False


def iter_audio_files(root: Path) -> list[Path]:
    """All audio files beneath ``root``, ignoring excluded directories."""
    extensions = settings.extensions
    found: list[Path] = []
    if not root.exists():
        log.warning("music directory does not exist: %s", root)
        return found

    for path in root.rglob("*"):
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        if path.suffix.lower().lstrip(".") not in extensions:
            continue
        if is_ignored(path.relative_to(root) if path.is_relative_to(root) else path):
            continue
        found.append(path)
    return found


# ─── Cover art ─────────────────────────────────────────────────────────────


def _cover_cache_path(key: str) -> Path:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return settings.covers_dir / f"{digest}.jpg"


def _find_folder_cover(folder: Path) -> Path | None:
    """Look for cover.jpg / folder.png / … next to the audio files."""
    if not folder.is_dir():
        return None
    wanted = settings.cover_names
    candidates: list[tuple[int, Path]] = []
    try:
        for entry in folder.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
                continue
            stem = entry.stem.lower()
            for rank, name in enumerate(wanted):
                if stem == name or stem.startswith(name):
                    candidates.append((rank, entry))
                    break
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _store_embedded_art(track_path: Path, key: str) -> Path | None:
    """Extract embedded art once and cache it as a normalised JPEG."""
    target = _cover_cache_path(key)
    if target.exists():
        return target
    art = extract_embedded_art(track_path)
    if not art:
        return None
    data, _mime = art
    try:
        from io import BytesIO

        from PIL import Image

        image = Image.open(BytesIO(data))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.save(target, "JPEG", quality=90)
        return target
    except Exception as exc:
        log.debug("could not normalise embedded art for %s: %s", track_path, exc)
        try:
            target.write_bytes(data)
            return target
        except OSError:
            return None


def resolve_cover_art(track_path: Path, tags: TrackTags, album_key: str) -> Path | None:
    """Folder art wins over embedded art — it is usually higher resolution."""
    folder_cover = _find_folder_cover(track_path.parent)
    if folder_cover:
        return folder_cover
    if tags.has_embedded_art:
        return _store_embedded_art(track_path, album_key)
    return None


# ─── Entity upserts ────────────────────────────────────────────────────────


def _get_or_create_genre(db: Session, name: str) -> Genre | None:
    name = name.strip()
    if not name:
        return None
    genre = db.scalar(select(Genre).where(Genre.name == name))
    if genre is None:
        genre = Genre(name=name)
        db.add(genre)
        db.flush()
    return genre


def _sort_key(value: str) -> str:
    """Strip a leading article so 'The Beatles' files under B."""
    lowered = value.strip()
    for article in ("The ", "A ", "An "):
        if lowered.startswith(article):
            return lowered[len(article):] + ", " + article.strip()
    return lowered


def get_or_create_artist(db: Session, name: str, mbid: str = "") -> Artist:
    name = (name or "Unknown Artist").strip() or "Unknown Artist"
    artist = db.scalar(select(Artist).where(Artist.name == name))
    if artist is None:
        artist = Artist(name=name, sort_name=_sort_key(name), mbid=mbid or None)
        db.add(artist)
        db.flush()
    elif mbid and not artist.mbid:
        artist.mbid = mbid
    return artist


def _album_key(tags: TrackTags, folder: Path) -> str:
    """Identity used to group tracks into an album.

    MusicBrainz release IDs are authoritative when present. Otherwise fall back
    to album-artist + album name, which keeps a compilation from splintering
    across its many track artists.
    """
    if settings.album_grouping == "musicbrainz" and tags.mb_release_id:
        return f"mb:{tags.mb_release_id}"
    return f"name:{(tags.album_artist or tags.artist).lower()}|{tags.album.lower()}"


def get_or_create_album(
    db: Session, tags: TrackTags, artist: Artist, folder: Path, cache: dict[str, Album]
) -> Album:
    key = _album_key(tags, folder)
    if key in cache:
        return cache[key]

    stmt = select(Album)
    if key.startswith("mb:"):
        stmt = stmt.where(Album.mbid == tags.mb_release_id)
    else:
        stmt = stmt.where(
            func.lower(Album.name) == tags.album.lower(),
            func.lower(Album.album_artist) == (tags.album_artist or tags.artist).lower(),
        )
    album = db.scalar(stmt)

    if album is None:
        album = Album(
            name=tags.album,
            sort_name=_sort_key(tags.sort_album or tags.album),
            artist_id=artist.id,
            artist_name=artist.name,
            album_artist=tags.album_artist or tags.artist,
            mbid=tags.mb_release_id or None,
            mb_release_group_id=tags.mb_release_group_id or None,
            year=tags.year,
            release_date=tags.release_date or None,
            genre=tags.genre,
            compilation=tags.compilation,
            folder_path=str(folder),
        )
        db.add(album)
        db.flush()
    else:
        # Backfill anything that was missing when the album was first created
        if tags.year and not album.year:
            album.year = tags.year
        if tags.genre and not album.genre:
            album.genre = tags.genre
        if tags.mb_release_id and not album.mbid:
            album.mbid = tags.mb_release_id
        if not album.folder_path:
            album.folder_path = str(folder)

    cache[key] = album
    return album


# ─── Track upsert ──────────────────────────────────────────────────────────


def upsert_track(
    db: Session,
    path: Path,
    *,
    album_cache: dict[str, Album],
    existing: Track | None = None,
) -> tuple[Track | None, str]:
    """Create or update one track. Returns ``(track, "added"|"updated"|"skipped")``."""
    try:
        stat = path.stat()
    except OSError as exc:
        log.warning("cannot stat %s: %s", path, exc)
        return None, "error"

    if existing is not None and abs(existing.mtime - stat.st_mtime) < 1.0:
        return existing, "skipped"

    tags = read_tags(path)
    if tags is None:
        return None, "error"

    artist = get_or_create_artist(db, tags.artist, tags.mb_artist_id)
    album_artist = (
        artist
        if (tags.album_artist or tags.artist) == tags.artist
        else get_or_create_artist(db, tags.album_artist, tags.mb_album_artist_id)
    )
    album = get_or_create_album(db, tags, album_artist, path.parent, album_cache)

    cover = resolve_cover_art(path, tags, _album_key(tags, path.parent))
    if cover and not album.cover_art_path:
        album.cover_art_path = str(cover)

    track = existing
    action = "updated"
    if track is None:
        track = Track(path=str(path))
        db.add(track)
        action = "added"

    track.title = tags.title
    track.sort_title = tags.sort_title or tags.title
    track.album_id = album.id
    track.artist_id = artist.id
    track.artist_name = artist.name
    track.album_name = album.name
    track.album_artist = tags.album_artist or tags.artist
    track.track_number = tags.track_number
    track.disc_number = tags.disc_number
    track.year = tags.year
    track.genre = tags.genre
    track.duration = tags.duration
    track.bitrate = tags.bitrate
    track.sample_rate = tags.sample_rate
    track.channels = tags.channels
    track.size = tags.size
    track.suffix = tags.suffix
    track.content_type = tags.content_type
    track.bpm = tags.bpm
    track.mbid = tags.mb_recording_id or None
    track.mb_release_id = tags.mb_release_id or None
    track.mb_artist_id = tags.mb_artist_id or None
    track.has_cover_art = bool(cover)
    track.cover_art_path = str(cover) if cover else None
    track.lyrics = tags.lyrics or None
    track.comment = tags.comment or None
    track.mtime = stat.st_mtime
    track.updated_at = utcnow()

    # Genres: replace wholesale so a retag drops stale values
    genre_objects = [g for g in (_get_or_create_genre(db, n) for n in tags.genres) if g]
    track.genres = genre_objects
    for genre in genre_objects:
        if genre not in album.genres:
            album.genres.append(genre)

    db.flush()
    return track, action


# ─── Aggregates ────────────────────────────────────────────────────────────


def refresh_aggregates(db: Session) -> None:
    """Recompute album/artist rollups after a scan."""
    album_rows = db.execute(
        select(
            Track.album_id,
            func.count(Track.id),
            func.coalesce(func.sum(Track.duration), 0),
            func.coalesce(func.sum(Track.size), 0),
            func.coalesce(func.max(Track.disc_number), 1),
        ).group_by(Track.album_id)
    ).all()

    for album_id, count, duration, size, disc_count in album_rows:
        if album_id is None:
            continue
        db.execute(
            update(Album)
            .where(Album.id == album_id)
            .values(
                song_count=count,
                duration=int(duration or 0),
                size=int(size or 0),
                disc_count=int(disc_count or 1),
            )
        )

    artist_rows = db.execute(
        select(
            Track.artist_id,
            func.count(Track.id),
            func.count(func.distinct(Track.album_id)),
        ).group_by(Track.artist_id)
    ).all()

    for artist_id, track_count, album_count in artist_rows:
        if artist_id is None:
            continue
        db.execute(
            update(Artist)
            .where(Artist.id == artist_id)
            .values(track_count=track_count, album_count=album_count)
        )

    # Drop albums and artists that no longer have any tracks
    empty_albums = db.scalars(
        select(Album.id).where(~Album.id.in_(select(Track.album_id).where(Track.album_id.isnot(None))))
    ).all()
    if empty_albums:
        db.execute(delete(Album).where(Album.id.in_(empty_albums)))

    empty_artists = db.scalars(
        select(Artist.id).where(
            ~Artist.id.in_(select(Track.artist_id).where(Track.artist_id.isnot(None))),
            ~Artist.id.in_(select(Album.artist_id).where(Album.artist_id.isnot(None))),
        )
    ).all()
    if empty_artists:
        db.execute(delete(Artist).where(Artist.id.in_(empty_artists)))

    db.commit()


# ─── Public scan entry points ──────────────────────────────────────────────


def scan_library(full: bool = False) -> ScanResult:
    """Reconcile the whole library against disk.

    ``full=True`` re-reads tags even for files whose mtime has not moved, which
    is what you want after changing tag-parsing behaviour.
    """
    if not _scan_lock.acquire(blocking=False):
        log.info("scan already in progress, skipping")
        return ScanResult()

    result = ScanResult()
    run_id: int | None = None
    try:
        with session_scope() as db:
            run = ScanRun(scanning=True, full_scan=full)
            db.add(run)
            db.flush()
            run_id = run.id

        _scan_state.update({"scanning": True, "count": 0, "total": 0, "run_id": run_id})
        log.info("scanning %s (full=%s)", settings.music_dir, full)

        files = iter_audio_files(settings.music_dir)
        _scan_state["total"] = len(files)
        log.info("found %d audio files", len(files))

        with session_scope() as db:
            existing_tracks = {
                path: (track_id, mtime)
                for path, track_id, mtime in db.execute(
                    select(Track.path, Track.id, Track.mtime)
                ).all()
            }

            album_cache: dict[str, Album] = {}
            seen_paths: set[str] = set()

            for index, path in enumerate(files, start=1):
                path_str = str(path)
                seen_paths.add(path_str)
                result.seen += 1

                record = existing_tracks.get(path_str)
                existing = db.get(Track, record[0]) if record else None
                if full and existing is not None:
                    existing.mtime = 0.0  # force a re-read

                try:
                    _track, action = upsert_track(
                        db, path, album_cache=album_cache, existing=existing
                    )
                except Exception as exc:
                    log.exception("failed to index %s: %s", path, exc)
                    result.errors += 1
                    db.rollback()
                    continue

                if action == "added":
                    result.added += 1
                elif action == "updated":
                    result.updated += 1
                elif action == "error":
                    result.errors += 1

                _scan_state["count"] = index
                if index % 200 == 0:
                    db.commit()
                    log.info("scanned %d/%d", index, len(files))

            db.commit()

            # Anything in the DB that is no longer on disk
            stale = [p for p in existing_tracks if p not in seen_paths]
            if stale:
                for chunk_start in range(0, len(stale), 500):
                    chunk = stale[chunk_start:chunk_start + 500]
                    db.execute(delete(Track).where(Track.path.in_(chunk)))
                result.removed = len(stale)
                db.commit()
                log.info("removed %d tracks that are no longer on disk", len(stale))

        with session_scope() as db:
            refresh_aggregates(db)

        log.info(
            "scan complete: %d seen, %d added, %d updated, %d removed, %d errors",
            result.seen, result.added, result.updated, result.removed, result.errors,
        )

        import_playlist_files()
    except Exception as exc:
        log.exception("scan failed: %s", exc)
        if run_id:
            with session_scope() as db:
                run = db.get(ScanRun, run_id)
                if run:
                    run.error = str(exc)
        raise
    finally:
        if run_id:
            with session_scope() as db:
                run = db.get(ScanRun, run_id)
                if run:
                    run.scanning = False
                    run.finished_at = utcnow()
                    run.tracks_seen = result.seen
                    run.tracks_added = result.added
                    run.tracks_updated = result.updated
                    run.tracks_removed = result.removed
        _scan_state.update({"scanning": False})
        _scan_lock.release()

    return result


def import_playlist_files(*, force: bool = False) -> None:
    """Pick up any ``.m3u`` sitting in the library.

    Called at the end of a scan rather than during it: an entry can only be
    resolved once the audio it names has been indexed, and a downloader drops
    the playlist file in the same breath as the tracks.
    """
    if not settings.playlist_auto_import:
        return
    try:
        from .playlistfile import import_all

        import_all(force=force)
    except Exception:
        log.exception("playlist import failed")


def scan_paths(paths: list[Path]) -> ScanResult:
    """Targeted rescan of individual files or directories."""
    result = ScanResult()
    targets: list[Path] = []
    for path in paths:
        if path.is_dir():
            targets.extend(iter_audio_files(path))
        elif path.is_file() and path.suffix.lower().lstrip(".") in settings.extensions:
            targets.append(path)

    if not targets:
        return result

    with session_scope() as db:
        album_cache: dict[str, Album] = {}
        for path in targets:
            existing = db.scalar(select(Track).where(Track.path == str(path)))
            try:
                _track, action = upsert_track(
                    db, path, album_cache=album_cache, existing=existing
                )
            except Exception as exc:
                log.exception("failed to index %s: %s", path, exc)
                result.errors += 1
                db.rollback()
                continue
            result.seen += 1
            if action == "added":
                result.added += 1
            elif action == "updated":
                result.updated += 1
        db.commit()

    with session_scope() as db:
        refresh_aggregates(db)
    return result


def remove_paths(paths: list[Path]) -> int:
    """Drop tracks whose files have been deleted."""
    removed = 0
    with session_scope() as db:
        for path in paths:
            count = db.execute(
                delete(Track).where(
                    (Track.path == str(path)) | (Track.path.like(f"{path}/%"))
                )
            ).rowcount
            removed += count or 0
        db.commit()
    if removed:
        with session_scope() as db:
            refresh_aggregates(db)
    return removed
