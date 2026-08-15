"""The "don't suggest this" index.

Five things disqualify a track from ever appearing as a recommendation:

* it is in your scrobble history — you already have a way to play it
* Musicdrome already downloaded it
* you dismissed it with ✕
* it was found in ``EXCLUDE_MUSIC_DIR``, an existing library you point at
* you hearted it in Navidrome, which means you own it and thought so hard
  about it that you went and starred it

Only *hearted* Navidrome tracks are excluded, not everything Navidrome knows
about. The play-count walk reads the whole library, so excluding all of it
would be a one-line change — and a much larger decision than it looks, since it
would silently suppress every recommendation of anything already on the disk
whether or not you had ever played it. ``EXCLUDE_MUSIC_DIR`` is the setting for
that, and it is opt-in for exactly that reason.

The folder scan reads artist and title tags and nothing else. No library
database is built, no files are moved, and the directory is never written to.

Every comparison goes through :func:`app.norm.track_key`, so an alias spelling
in one source still matches the canonical one in another.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from . import config, db
from .norm import artist_key, track_key

log = logging.getLogger(__name__)

AUDIO_SUFFIXES = {".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".m4b", ".aac", ".wma", ".wav"}
IGNORE_DIRS = {"@eaDir", ".AppleDouble", "#recycle", ".stfolder", "lost+found", "_playlists"}


def build() -> set[str]:
    """Every track key that must not be suggested."""
    keys: set[str] = set()
    with db.connect() as conn:
        keys.update(row[0] for row in conn.execute("SELECT DISTINCT track_key FROM plays"))
        # Completed downloads only. A *failed* download must not exclude the
        # track — the failure is usually about YouTube, not about the music,
        # and counting it would silently blacklist the track forever.
        keys.update(
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT track_key FROM downloads WHERE status = 'done'"
            )
        )
        keys.update(
            row[0]
            for row in conn.execute(
                "SELECT track_key FROM suggestions WHERE status IN ('hidden', 'downloaded', 'queued', 'downloading')"
            )
        )
        keys.update(row[0] for row in conn.execute("SELECT DISTINCT track_key FROM excluded_files"))
        keys.update(
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT track_key FROM navidrome_tracks WHERE starred = 1"
            )
        )
    keys.discard("")
    return keys


def known_artists(limit: int = 400) -> set[str]:
    """Artist keys already represented in the library or history.

    Used to soften the prompt rather than to filter: a new track by an artist
    you already listen to is a perfectly good recommendation, it just should not
    crowd out everything else.
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT artist_key, COUNT(*) AS plays FROM plays "
            "GROUP BY artist_key ORDER BY plays DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return {row["artist_key"] for row in rows if row["artist_key"]}


# ─── Library folder scan ───────────────────────────────────────────────────


def _read_tags(path: Path) -> tuple[str, str]:
    """Artist and title from a file's tags, falling back to its name."""
    try:
        import mutagen
    except ImportError:  # pragma: no cover - mutagen is a pinned dependency
        return "", ""

    try:
        audio = mutagen.File(path, easy=True)
    except Exception:
        return "", ""

    if audio is not None and audio.tags:
        artist = (audio.tags.get("artist") or [""])[0]
        title = (audio.tags.get("title") or [""])[0]
        if artist and title:
            return artist, title

    # Untagged rips are usually "Artist - Title.mp3" or ".../Artist/Album/NN - Title.mp3"
    stem = path.stem
    if " - " in stem:
        left, right = stem.split(" - ", 1)
        if left.strip().isdigit() and len(path.parts) >= 3:
            return path.parts[-3], right.strip()
        return left.strip(), right.strip()
    return "", ""


def scan_library(root: str | None = None) -> dict[str, int]:
    """Refresh the exclusion index from ``EXCLUDE_MUSIC_DIR``.

    Files already indexed at their current mtime are skipped, so a repeat scan
    over a large library costs a stat per file rather than a tag read.
    """
    directory = root if root is not None else config.EXCLUDE_MUSIC_DIR
    stats = {"seen": 0, "indexed": 0, "removed": 0}
    if not directory:
        return stats

    base = Path(directory).expanduser()
    if not base.is_dir():
        log.warning("EXCLUDE_MUSIC_DIR %s is not a directory — skipping", base)
        return stats

    with db.connect() as conn:
        known = {
            row["path"]: row["mtime"]
            for row in conn.execute("SELECT path, mtime FROM excluded_files")
        }

        present: set[str] = set()
        pending: list[tuple[str, float, str, str]] = []

        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")]
            for filename in filenames:
                path = Path(dirpath) / filename
                if path.suffix.lower() not in AUDIO_SUFFIXES:
                    continue
                key = str(path)
                present.add(key)
                stats["seen"] += 1
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if known.get(key) == mtime:
                    continue

                artist, title = _read_tags(path)
                if not artist or not title:
                    continue
                pending.append((key, mtime, track_key(artist, title), artist_key(artist)))

                if len(pending) >= 500:
                    _flush(conn, pending)
                    stats["indexed"] += len(pending)
                    pending.clear()

        if pending:
            _flush(conn, pending)
            stats["indexed"] += len(pending)

        gone = [path for path in known if path not in present]
        for chunk in (gone[i : i + 500] for i in range(0, len(gone), 500)):
            conn.executemany("DELETE FROM excluded_files WHERE path = ?", [(p,) for p in chunk])
        stats["removed"] = len(gone)

    log.info(
        "exclusion index: %d files seen, %d indexed, %d removed",
        stats["seen"], stats["indexed"], stats["removed"],
    )
    return stats


def _flush(conn, rows: list[tuple[str, float, str, str]]) -> None:
    conn.executemany(
        "INSERT INTO excluded_files (path, mtime, track_key, artist_key) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (path) DO UPDATE SET mtime = excluded.mtime, "
        "track_key = excluded.track_key, artist_key = excluded.artist_key",
        rows,
    )
