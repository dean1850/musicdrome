"""Finding the audio for a suggested track, and filing it.

Two searches, in order. YouTube Music first, through ``ytmusicapi``: it returns
a real catalogue with artist, album and duration as structured fields, so a
candidate can be checked rather than guessed at. Plain YouTube second, through
yt-dlp, for the tracks YouTube Music does not carry — that path has only a title
string to go on, so it leans on keyword scoring and is trusted less.

Nothing is downloaded on a weak match. A card that cannot be matched confidently
is marked failed with the reason, which is a better outcome than quietly filing
a karaoke version into the library.

Downloads run on a small pool of worker threads draining the ``downloads``
table, so the queue survives a restart: anything left mid-flight is requeued at
boot by :func:`start_workers`.
"""

from __future__ import annotations

import logging
import queue
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from . import config, db
from .norm import artist_key, safe_filename, title_key, track_key

log = logging.getLogger(__name__)

# Titles that usually mean "not the studio recording that was asked for".
NEGATIVE = re.compile(
    r"\b(live|cover|karaoke|instrumental|remix|reaction|lyrics?\s*video|"
    r"sped\s*up|slowed|nightcore|8d\s*audio|tutorial|reverb|mashup|"
    r"full\s*album|mix\s*\d{4}|extended\s*mix)\b",
    re.IGNORECASE,
)

MIN_SCORE = 0.50   # below this, no download is attempted at all
GOOD_SCORE = 0.75  # above this on YouTube Music, plain YouTube is not consulted

_queue: "queue.Queue[int]" = queue.Queue()
_workers: list[threading.Thread] = []
_progress: dict[int, int] = {}
_ytmusic_client: Any = None
_ytmusic_lock = threading.Lock()


class DownloadError(RuntimeError):
    pass


@dataclass
class Candidate:
    url: str
    title: str
    artist: str = ""
    album: str = ""
    duration: int = 0
    source: str = "youtube"
    score: float = field(default=0.0)


# ─── Candidate scoring ─────────────────────────────────────────────────────


def score(candidate: Candidate, artist: str, title: str, expected: int = 0) -> float:
    """How much this candidate looks like the track we asked for, 0-1.

    Duration is the strongest signal available: two recordings that agree on
    artist, title and length to within a few seconds are almost always the same
    master, and a "cover" that agrees on all three is one worth having anyway.
    """
    want_artist, want_title = artist_key(artist), title_key(title)
    haystack = f"{candidate.artist} {candidate.title} {candidate.album}".casefold()
    value = 0.0
    attributed = True

    if candidate.artist and artist_key(candidate.artist) == want_artist:
        value += 0.45
    elif want_artist and want_artist in artist_key(haystack):
        value += 0.30
    else:
        attributed = False

    if title_key(candidate.title) == want_title:
        value += 0.35
    elif want_title and want_title in title_key(candidate.title):
        value += 0.20

    # Only penalise "live"/"remix" when the track we want is not itself one.
    if NEGATIVE.search(candidate.title) and not NEGATIVE.search(f"{artist} {title}"):
        value -= 0.40

    if expected and candidate.duration:
        drift = abs(candidate.duration - expected)
        value += 0.25 if drift <= 3 else 0.15 if drift <= 10 else 0.0 if drift <= 25 else -0.35
    elif candidate.duration and not (45 <= candidate.duration <= 900):
        value -= 0.30  # without a reference, reject things that are not one track

    if candidate.source == "ytmusic":
        value += 0.05

    # A candidate we cannot attribute to the right artist is never downloaded,
    # however well the title and length line up. That combination is what a
    # tribute band, a re-upload or a soundalike looks like, and filing one of
    # those into the library is worse than coming back empty-handed.
    if not attributed:
        value = min(value, MIN_SCORE - 0.05)

    return max(0.0, min(1.0, value))


# ─── Search ────────────────────────────────────────────────────────────────


def _ytmusic():
    global _ytmusic_client
    with _ytmusic_lock:
        if _ytmusic_client is None:
            from ytmusicapi import YTMusic

            _ytmusic_client = YTMusic()
        return _ytmusic_client


def search_ytmusic(artist: str, title: str, limit: int = 8) -> list[Candidate]:
    """Search the YouTube Music catalogue."""
    try:
        results = _ytmusic().search(f"{artist} {title}", filter="songs", limit=limit)
    except Exception as exc:
        log.warning("YouTube Music search failed for %s - %s: %s", artist, title, exc)
        return []

    candidates = []
    for entry in results or []:
        video_id = entry.get("videoId")
        if not video_id:
            continue
        candidates.append(
            Candidate(
                url=f"https://music.youtube.com/watch?v={video_id}",
                title=entry.get("title", ""),
                artist=", ".join(a.get("name", "") for a in entry.get("artists") or []),
                album=(entry.get("album") or {}).get("name", ""),
                duration=int(entry.get("duration_seconds") or 0),
                source="ytmusic",
            )
        )
    return candidates


def _ydl_options(**overrides) -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "retries": 3,
        "socket_timeout": 30,
    }
    if config.YTDLP_PROXY:
        options["proxy"] = config.YTDLP_PROXY
    if config.YTDLP_COOKIES_FILE:
        options["cookiefile"] = config.YTDLP_COOKIES_FILE
    if config.YTDLP_RATE_LIMIT:
        try:
            options["ratelimit"] = int(config.YTDLP_RATE_LIMIT)
        except ValueError:
            pass
    options.update(overrides)
    return options


def search_youtube(artist: str, title: str, limit: int = 5) -> list[Candidate]:
    """Fall back to a plain YouTube search."""
    import yt_dlp

    try:
        with yt_dlp.YoutubeDL(_ydl_options(skip_download=True, extract_flat=False)) as ydl:
            info = ydl.extract_info(f"ytsearch{limit}:{artist} {title} audio", download=False)
    except Exception as exc:
        log.warning("YouTube search failed for %s - %s: %s", artist, title, exc)
        return []

    entries = (info or {}).get("entries") or []
    candidates = []
    for entry in entries:
        url = (entry or {}).get("webpage_url") or (entry or {}).get("url")
        if not url:
            continue
        candidates.append(
            Candidate(
                url=url,
                title=entry.get("title", ""),
                artist=entry.get("artist") or entry.get("uploader") or "",
                album=entry.get("album") or "",
                duration=int(entry.get("duration") or 0),
                source="youtube",
            )
        )
    return candidates


def best_match(artist: str, title: str, expected: int = 0) -> Candidate | None:
    """The best candidate across both sources, or ``None`` if none is credible."""
    ranked: list[Candidate] = []

    for candidate in search_ytmusic(artist, title):
        candidate.score = score(candidate, artist, title, expected)
        ranked.append(candidate)

    ranked.sort(key=lambda c: c.score, reverse=True)
    if ranked and ranked[0].score >= GOOD_SCORE:
        return ranked[0]

    for candidate in search_youtube(artist, title):
        candidate.score = score(candidate, artist, title, expected)
        ranked.append(candidate)

    ranked.sort(key=lambda c: c.score, reverse=True)
    if ranked and ranked[0].score >= MIN_SCORE:
        return ranked[0]
    return None


# ─── Tagging and filing ────────────────────────────────────────────────────


def _fetch_cover(url: str) -> bytes:
    if not url:
        return b""
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            response = client.get(url)
        if response.status_code == 200 and len(response.content) < 8_000_000:
            return response.content
    except httpx.HTTPError:
        pass
    return b""


def tag(path: Path, meta: dict[str, Any]) -> None:
    """Write ID3 tags and embed the cover art."""
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import APIC, ID3, ID3NoHeaderError
    from mutagen.mp3 import MP3

    try:
        audio = EasyID3(path)
    except ID3NoHeaderError:
        mp3 = MP3(path)
        mp3.add_tags()
        mp3.save()
        audio = EasyID3(path)

    fields = {
        "artist": meta.get("artist", ""),
        "title": meta.get("title", ""),
        "album": meta.get("album", ""),
        "albumartist": meta.get("artist", ""),
        "date": meta.get("year", ""),
        "genre": (meta.get("tags") or "").split(",")[0],
    }
    if meta.get("track_no"):
        fields["tracknumber"] = str(meta["track_no"])
    if meta.get("recording_mbid"):
        fields["musicbrainz_trackid"] = meta["recording_mbid"]

    for key, value in fields.items():
        if value:
            audio[key] = str(value)
    audio.save()

    cover = _fetch_cover(meta.get("cover_url", ""))
    if cover:
        try:
            id3 = ID3(path)
            mime = "image/png" if cover[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
            id3.delall("APIC")
            id3.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover))
            id3.save(path)
        except Exception as exc:
            log.debug("could not embed cover art in %s: %s", path, exc)


def target_path(artist: str, album: str, title: str, track_no: int = 0) -> Path:
    """``MUSIC_DIR/Artist/Album/01 - Title.mp3``, never overwriting."""
    artist_part = safe_filename(artist, "Unknown Artist")
    album_part = safe_filename(album, "Singles")
    title_part = safe_filename(title, "Untitled")
    prefix = f"{track_no:02d} - " if track_no else ""

    directory = config.MUSIC_DIR / artist_part / album_part
    directory.mkdir(parents=True, exist_ok=True)

    target = directory / f"{prefix}{title_part}.mp3"
    if not target.exists():
        return target
    for index in range(2, 100):
        alternative = directory / f"{prefix}{title_part} ({index}).mp3"
        if not alternative.exists():
            return alternative
    raise DownloadError(f"too many files named {title_part} in {directory}")


def append_to_playlist(scan_id: int | None, path: Path, meta: dict[str, Any]) -> str:
    """Add a finished download to its scan's .m3u, creating it on first use.

    Paths are written relative to the playlist file so the folder can be moved
    or mounted elsewhere and still play.
    """
    if not scan_id:
        return ""

    config.PLAYLIST_DIR.mkdir(parents=True, exist_ok=True)
    playlist = config.PLAYLIST_DIR / f"musicdrome-scan-{scan_id:04d}.m3u"

    try:
        relative = path.relative_to(config.PLAYLIST_DIR.parent)
        entry = Path("..") / relative
    except ValueError:
        entry = path

    new_file = not playlist.exists()
    with playlist.open("a", encoding="utf-8") as handle:
        if new_file:
            handle.write("#EXTM3U\n")
        handle.write(
            f"#EXTINF:{meta.get('duration', 0)},{meta.get('artist', '')} - {meta.get('title', '')}\n"
            f"{entry.as_posix()}\n"
        )
    return str(playlist)


# ─── Fetching ──────────────────────────────────────────────────────────────


def fetch(download_id: int) -> None:
    """Run one queued download to completion, updating its row as it goes."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT d.*, s.year, s.track_no, s.tags, s.cover_url, s.recording_mbid, "
            "       s.duration AS want_duration, s.scan_id "
            "FROM downloads d LEFT JOIN suggestions s ON s.id = d.suggestion_id "
            "WHERE d.id = ?",
            (download_id,),
        ).fetchone()
    if row is None or row["status"] not in {"queued", "downloading"}:
        return

    meta = dict(row)
    _set_status(download_id, "downloading")
    _progress[download_id] = 0

    try:
        if not config.MUSIC_DIR.exists():
            raise DownloadError(f"MUSIC_DIR {config.MUSIC_DIR} does not exist")

        candidate = best_match(meta["artist"], meta["title"], int(meta["want_duration"] or 0))
        if candidate is None:
            raise DownloadError("no confident match on YouTube Music or YouTube")

        log.info(
            "downloading %s — %s from %s (score %.2f)",
            meta["artist"], meta["title"], candidate.source, candidate.score,
        )
        path = _download_audio(download_id, candidate, meta)
        playlist = append_to_playlist(meta["scan_id"], path, meta)

        with db.connect() as conn:
            conn.execute(
                "UPDATE downloads SET status = 'done', path = ?, source_url = ?, source = ?, "
                "bytes = ?, duration = ?, progress = 100, error = '', finished_at = ? WHERE id = ?",
                (
                    str(path), candidate.url, candidate.source,
                    path.stat().st_size, candidate.duration, db.now(), download_id,
                ),
            )
            if meta["suggestion_id"]:
                conn.execute(
                    "UPDATE suggestions SET status = 'downloaded', decided_at = ? WHERE id = ?",
                    (db.now(), meta["suggestion_id"]),
                )
            if playlist and meta["scan_id"]:
                conn.execute(
                    "UPDATE scans SET playlist_path = ? WHERE id = ?", (playlist, meta["scan_id"])
                )
        log.info("imported %s", path)

    except Exception as exc:
        message = str(exc)[:500]
        log.warning("download %d failed: %s", download_id, message)
        with db.connect() as conn:
            conn.execute(
                "UPDATE downloads SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                (message, db.now(), download_id),
            )
            if meta["suggestion_id"]:
                conn.execute(
                    "UPDATE suggestions SET status = 'failed', error = ? WHERE id = ?",
                    (message, meta["suggestion_id"]),
                )
    finally:
        _progress.pop(download_id, None)


def _download_audio(download_id: int, candidate: Candidate, meta: dict[str, Any]) -> Path:
    """yt-dlp into a temp dir, transcode to 320 kbps MP3, tag, then file it."""
    import yt_dlp

    def hook(status: dict) -> None:
        if status.get("status") != "downloading":
            return
        total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
        if total:
            _progress[download_id] = min(99, int(status.get("downloaded_bytes", 0) * 100 / total))

    workdir = Path(tempfile.mkdtemp(prefix="musicdrome-", dir=config.DATA_DIR))
    try:
        options = _ydl_options(
            outtmpl=str(workdir / "%(id)s.%(ext)s"),
            progress_hooks=[hook],
            postprocessors=[
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    # Not 0-9, so yt-dlp passes this to ffmpeg as -b:a 320k.
                    "preferredquality": config.AUDIO_BITRATE,
                }
            ],
            ffmpeg_location=str(Path(config.FFMPEG_PATH).parent),
        )
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.extract_info(candidate.url, download=True)

        produced = sorted(
            (p for p in workdir.iterdir() if p.is_file() and p.suffix.lower() == ".mp3"),
            key=lambda p: p.stat().st_size,
            reverse=True,
        )
        if not produced:
            raise DownloadError("yt-dlp produced no MP3 — is ffmpeg installed?")

        source = produced[0]
        tag(source, meta)

        target = target_path(
            meta["artist"], meta["album"], meta["title"], int(meta["track_no"] or 0)
        )
        shutil.move(str(source), str(target))
        return target
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _set_status(download_id: int, status: str) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE downloads SET status = ? WHERE id = ?", (status, download_id))


# ─── Queue ─────────────────────────────────────────────────────────────────


def downloads_today() -> int:
    since = db.now() - 86400
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM downloads WHERE status = 'done' AND finished_at >= ?",
            (since,),
        ).fetchone()
    return row["n"] if row else 0


def enqueue(suggestion_id: int) -> int | None:
    """Queue one suggestion for download. Returns the download id, or None."""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
        if row is None:
            return None
        existing = conn.execute(
            "SELECT id FROM downloads WHERE suggestion_id = ? AND status IN "
            "('queued', 'downloading', 'done')",
            (suggestion_id,),
        ).fetchone()
        if existing:
            return existing["id"]

        cursor = conn.execute(
            "INSERT INTO downloads (suggestion_id, track_key, artist, title, album, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                suggestion_id, row["track_key"], row["artist"], row["title"],
                row["album"], db.now(),
            ),
        )
        download_id = cursor.lastrowid
        conn.execute(
            "UPDATE suggestions SET status = 'queued', error = '', decided_at = ? WHERE id = ?",
            (db.now(), suggestion_id),
        )

    _queue.put(download_id)
    return download_id


def auto_enqueue(scan_id: int | None = None) -> int:
    """Queue high-confidence suggestions, if auto-download is switched on.

    The daily cap counts finished downloads in the last 24 hours, so a runaway
    scan cannot fill a disk overnight — it just stops queueing.
    """
    settings = db.get_settings()
    if not settings["auto_download"]:
        return 0

    remaining = int(settings["daily_download_cap"]) - downloads_today()
    if remaining <= 0:
        log.info("daily download cap of %s reached", settings["daily_download_cap"])
        return 0

    query = (
        "SELECT id FROM suggestions WHERE status = 'new' AND match >= ? "
        + ("AND scan_id = ? " if scan_id else "")
        + "ORDER BY match DESC LIMIT ?"
    )
    params: list[Any] = [int(settings["auto_download_threshold"])]
    if scan_id:
        params.append(scan_id)
    params.append(remaining)

    with db.connect() as conn:
        ids = [row["id"] for row in conn.execute(query, params)]

    queued = sum(1 for suggestion_id in ids if enqueue(suggestion_id))
    if queued:
        log.info("auto-queued %d downloads above %s%%", queued, settings["auto_download_threshold"])
    return queued


def active() -> list[dict[str, Any]]:
    """In-flight downloads, for the UI's progress poll."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, artist, title, status FROM downloads "
            "WHERE status IN ('queued', 'downloading') ORDER BY id"
        ).fetchall()
    return [{**dict(row), "progress": _progress.get(row["id"], 0)} for row in rows]


def _worker() -> None:
    while True:
        download_id = _queue.get()
        try:
            fetch(download_id)
        except Exception:
            log.exception("download worker crashed on %s", download_id)
        finally:
            _queue.task_done()


def start_workers() -> None:
    """Start the pool and requeue anything interrupted by a restart."""
    if _workers:
        return

    with db.connect() as conn:
        conn.execute("UPDATE downloads SET status = 'queued' WHERE status = 'downloading'")
        pending = [
            row["id"]
            for row in conn.execute("SELECT id FROM downloads WHERE status = 'queued' ORDER BY id")
        ]

    for index in range(max(1, config.DOWNLOAD_CONCURRENCY)):
        thread = threading.Thread(target=_worker, name=f"musicdrome-download-{index}", daemon=True)
        thread.start()
        _workers.append(thread)

    for download_id in pending:
        _queue.put(download_id)
    if pending:
        log.info("requeued %d interrupted downloads", len(pending))


def retry(download_id: int) -> bool:
    """Put a failed download back on the queue."""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM downloads WHERE id = ?", (download_id,)).fetchone()
        if row is None or row["status"] not in {"failed", "done"}:
            return False
        conn.execute(
            "UPDATE downloads SET status = 'queued', error = '', progress = 0, finished_at = NULL "
            "WHERE id = ?",
            (download_id,),
        )
        if row["suggestion_id"]:
            conn.execute(
                "UPDATE suggestions SET status = 'queued', error = '' WHERE id = ?",
                (row["suggestion_id"],),
            )
    _queue.put(download_id)
    return True


def remove(download_id: int, delete_file: bool = False) -> bool:
    """Forget a download, optionally deleting the file it produced."""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM downloads WHERE id = ?", (download_id,)).fetchone()
        if row is None:
            return False
        if delete_file and row["path"]:
            path = Path(row["path"])
            try:
                # Only ever delete inside MUSIC_DIR, whatever the row claims.
                path.resolve().relative_to(config.MUSIC_DIR.resolve())
                path.unlink(missing_ok=True)
            except (ValueError, OSError) as exc:
                log.warning("refusing to delete %s: %s", path, exc)
        conn.execute("DELETE FROM downloads WHERE id = ?", (download_id,))
        if row["suggestion_id"]:
            conn.execute(
                "UPDATE suggestions SET status = 'new', error = '' WHERE id = ?",
                (row["suggestion_id"],),
            )
    return True


def track_is_downloaded(artist: str, title: str) -> bool:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM downloads WHERE track_key = ? AND status = 'done' LIMIT 1",
            (track_key(artist, title),),
        ).fetchone()
    return row is not None
