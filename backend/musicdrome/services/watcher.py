"""Filesystem watcher.

Copying an album in produces a burst of create/modify events — often several per
file. Rather than rescanning on each one, events are collected into a pending
set and flushed once the directory has been quiet for ``DEBOUNCE_SECONDS``.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from ..config import settings
from . import scanner

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 5.0


class _DebouncedHandler(FileSystemEventHandler):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._changed: set[Path] = set()
        self._deleted: set[Path] = set()
        self._timer: threading.Timer | None = None

    # ─── watchdog callbacks ───────────────────────────────────────────────

    def on_created(self, event: FileSystemEvent) -> None:
        self._record(event, deleted=False)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._record(event, deleted=False)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._record(event, deleted=True)

    def on_moved(self, event: FileSystemEvent) -> None:
        src = Path(str(event.src_path))
        dest = Path(str(getattr(event, "dest_path", "")))
        with self._lock:
            self._deleted.add(src)
            if dest:
                self._changed.add(dest)
        self._arm()

    # ─── internals ────────────────────────────────────────────────────────

    def _relevant(self, path: Path, *, deleted: bool) -> bool:
        if deleted:
            # A deleted path has no suffix guarantee — let the DB decide.
            return True
        if path.is_dir():
            return True
        suffix = path.suffix.lower().lstrip(".")
        return suffix in settings.extensions or suffix in settings.playlist_extensions

    @staticmethod
    def _is_playlist(path: Path) -> bool:
        return path.suffix.lower().lstrip(".") in settings.playlist_extensions

    def _record(self, event: FileSystemEvent, *, deleted: bool) -> None:
        path = Path(str(event.src_path))
        if not self._relevant(path, deleted=deleted):
            return
        with self._lock:
            (self._deleted if deleted else self._changed).add(path)
        self._arm()

    def _arm(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_SECONDS, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            changed = sorted(self._changed)
            deleted = sorted(self._deleted)
            self._changed.clear()
            self._deleted.clear()
            self._timer = None

        if deleted:
            try:
                count = scanner.remove_paths(deleted)
                if count:
                    log.info("watcher removed %d tracks", count)
            except Exception:
                log.exception("watcher failed to process deletions")

        indexed = False
        if changed:
            try:
                result = scanner.scan_paths(changed)
                indexed = bool(result.added or result.updated)
                if indexed:
                    log.info(
                        "watcher indexed %d new / %d updated tracks",
                        result.added, result.updated,
                    )
            except Exception:
                log.exception("watcher failed to process changes")

        # Audio first, playlists second: a downloader writes the .m3u alongside
        # the tracks, and its entries only resolve once those are indexed. New
        # audio alone is reason enough to re-run — it may complete a playlist
        # that imported with entries missing.
        touched_playlist = any(
            self._is_playlist(path) for path in (*changed, *deleted)
        )
        if touched_playlist or indexed:
            scanner.import_playlist_files()


class LibraryWatcher:
    """Owns the watchdog observer lifecycle."""

    def __init__(self) -> None:
        self._observer: Observer | None = None

    def start(self) -> None:
        if not settings.scan_watch_filesystem:
            log.info("filesystem watching disabled")
            return
        if not settings.music_dir.exists():
            log.warning("cannot watch %s — directory missing", settings.music_dir)
            return
        if self._observer is not None:
            return

        observer = Observer()
        handler = _DebouncedHandler()
        watched = [settings.music_dir]

        # Playlist roots outside the library — a downloader's output mounted
        # separately — are watched too, otherwise its .m3u only lands on the
        # next scheduled import instead of the moment it is written.
        if settings.playlist_auto_import:
            for root in settings.playlist_import_roots:
                if root not in watched and root.is_dir():
                    watched.append(root)

        for root in watched:
            observer.schedule(handler, str(root), recursive=True)

        observer.daemon = True
        observer.start()
        self._observer = observer
        log.info("watching %s for changes", ", ".join(str(root) for root in watched))

    def stop(self) -> None:
        if self._observer is None:
            return
        self._observer.stop()
        try:
            self._observer.join(timeout=5)
        except RuntimeError:
            pass
        self._observer = None
        log.info("stopped filesystem watcher")


watcher = LibraryWatcher()
