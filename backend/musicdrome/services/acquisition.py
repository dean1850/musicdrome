"""Track acquisition via yt-dlp.

Flow: a recommendation becomes a :class:`WantedItem` (status ``pending``), a
user approves it, and this module searches, downloads, tags and imports it.

Approval is the default gate — set ``AUTO_DOWNLOAD=true`` to have anything above
``ACQUISITION_MIN_CONFIDENCE`` approve itself. ``ACQUISITION_MAX_PER_DAY`` caps
unattended downloads either way, so a runaway recommendation loop cannot fill
the disk overnight.

Downloaded audio is tagged from MusicBrainz where a confident match exists, then
filed into ``MUSIC_DIR`` using ``ACQUISITION_IMPORT_TEMPLATE`` and handed to the
scanner.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import session_scope, utcnow
from ..models import Track, WantedItem, WantedStatus
from . import scanner
from .musicbrainz import musicbrainz

log = logging.getLogger(__name__)

_slots = threading.Semaphore(settings.acquisition_max_concurrent)

# Titles that usually mean "not the studio recording the user asked for"
_NEGATIVE = re.compile(
    r"\b(live|cover|karaoke|instrumental|remix|reaction|lyrics?\s+video|"
    r"sped\s*up|slowed|nightcore|8d\s*audio|tutorial|reverb)\b",
    re.IGNORECASE,
)
_UNSAFE_PATH = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class AcquisitionError(RuntimeError):
    pass


@dataclass
class Candidate:
    url: str
    title: str
    uploader: str
    duration: int
    score: float = 0.0


def _safe_component(value: str, fallback: str) -> str:
    cleaned = _UNSAFE_PATH.sub("", (value or "").strip()).strip(". ")
    return (cleaned or fallback)[:120]


# ─── Search ────────────────────────────────────────────────────────────────


def _ydl_options(**overrides) -> dict:
    options: dict = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "nocheckcertificate": False,
        "format": settings.ytdlp_format,
        "retries": 3,
        "socket_timeout": 30,
    }
    if settings.ytdlp_proxy:
        options["proxy"] = settings.ytdlp_proxy
    if settings.ytdlp_cookies_file:
        options["cookiefile"] = settings.ytdlp_cookies_file
    if settings.ytdlp_rate_limit:
        try:
            options["ratelimit"] = int(settings.ytdlp_rate_limit)
        except ValueError:
            pass
    options.update(overrides)
    return options


def _score(candidate: Candidate, artist: str, title: str, expected: int = 0) -> float:
    haystack = f"{candidate.title} {candidate.uploader}".lower()
    score = 0.0

    if artist and artist.lower() in haystack:
        score += 0.4
    if title and title.lower() in haystack:
        score += 0.4
    if _NEGATIVE.search(candidate.title) and not _NEGATIVE.search(f"{artist} {title}"):
        score -= 0.35
    if "topic" in candidate.uploader.lower() or "- topic" in haystack:
        score += 0.15  # auto-generated artist channels are usually the studio cut
    if "official" in haystack:
        score += 0.1

    if expected and candidate.duration:
        drift = abs(candidate.duration - expected) / max(expected, 1)
        score += 0.2 if drift < 0.1 else (0.1 if drift < 0.25 else -0.2)
    elif candidate.duration:
        # Without a reference, reject things that are obviously not one track
        if candidate.duration < 45 or candidate.duration > 900:
            score -= 0.3

    return max(0.0, min(1.0, score))


def search(query: str, *, artist: str = "", title: str = "", expected_duration: int = 0) -> list[Candidate]:
    """Search for downloadable candidates, best match first."""
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise AcquisitionError("yt-dlp is not installed") from exc

    prefix = settings.acquisition_search_prefix or "ytsearch5"
    search_term = query if "://" in query else f"{prefix}:{query}"

    try:
        with yt_dlp.YoutubeDL(_ydl_options(extract_flat=False, skip_download=True)) as ydl:
            info = ydl.extract_info(search_term, download=False)
    except Exception as exc:
        raise AcquisitionError(f"search failed: {exc}") from exc

    entries = info.get("entries") if isinstance(info, dict) else None
    if entries is None:
        entries = [info] if info else []

    candidates: list[Candidate] = []
    for entry in entries:
        if not entry:
            continue
        url = entry.get("webpage_url") or entry.get("url") or ""
        if not url:
            continue
        candidate = Candidate(
            url=url,
            title=entry.get("title", ""),
            uploader=entry.get("uploader") or entry.get("channel") or "",
            duration=int(entry.get("duration") or 0),
        )
        candidate.score = _score(candidate, artist, title, expected_duration)
        candidates.append(candidate)

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


# ─── Tagging and import ────────────────────────────────────────────────────


def _tag(path: Path, metadata: dict) -> None:
    """Write tags onto a freshly downloaded file."""
    try:
        from mutagen.easyid3 import EasyID3
        from mutagen.id3 import ID3NoHeaderError
        from mutagen.mp3 import MP3
    except ImportError:  # pragma: no cover
        return

    try:
        if path.suffix.lower() == ".mp3":
            try:
                audio = EasyID3(path)
            except ID3NoHeaderError:
                mp3 = MP3(path)
                mp3.add_tags()
                mp3.save()
                audio = EasyID3(path)
        else:
            import mutagen

            audio = mutagen.File(path, easy=True)
            if audio is None:
                return
            if audio.tags is None:
                audio.add_tags()

        for key, value in metadata.items():
            if value:
                audio[key] = str(value)
        audio.save()
    except Exception as exc:
        log.debug("could not tag %s: %s", path, exc)


def _import_path(artist: str, album: str, title: str, track_no: int, suffix: str) -> Path:
    template = settings.acquisition_import_template
    try:
        relative = template.format(
            artist=_safe_component(artist, "Unknown Artist"),
            album=_safe_component(album, "Unknown Album"),
            title=_safe_component(title, "Untitled"),
            track=track_no or 0,
            ext=suffix,
        )
    except (KeyError, ValueError, IndexError):
        log.warning("invalid ACQUISITION_IMPORT_TEMPLATE, falling back to default")
        relative = (
            f"{_safe_component(artist, 'Unknown Artist')}/"
            f"{_safe_component(album, 'Unknown Album')}/"
            f"{_safe_component(title, 'Untitled')}.{suffix}"
        )

    target = settings.music_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)

    # Never clobber an existing file
    if target.exists():
        stem, ext = target.stem, target.suffix
        for index in range(2, 100):
            alternative = target.with_name(f"{stem} ({index}){ext}")
            if not alternative.exists():
                return alternative
    return target


def download(item: WantedItem, *, url: str | None = None) -> Path:
    """Download, tag and import one wanted item. Returns the imported path."""
    if settings.music_read_only:
        raise AcquisitionError("MUSIC_DIR is mounted read-only")

    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover
        raise AcquisitionError("yt-dlp is not installed") from exc

    query = url or " ".join(
        part for part in (item.artist_name, item.title or item.album_name) if part
    ).strip()
    if not query:
        raise AcquisitionError("wanted item has neither an artist nor a title")

    if not url:
        candidates = search(query, artist=item.artist_name, title=item.title or "")
        if not candidates:
            raise AcquisitionError(f"no results for '{query}'")
        best = candidates[0]
        if best.score < 0.3:
            raise AcquisitionError(
                f"best match '{best.title}' scored {best.score:.2f} — too weak to trust"
            )
        url = best.url

    audio_format = settings.ytdlp_audio_format or "mp3"

    acquired = _slots.acquire(timeout=600)
    if not acquired:
        raise AcquisitionError("timed out waiting for a download slot")

    workdir = Path(tempfile.mkdtemp(prefix="musicdrome-", dir=settings.download_dir))
    try:
        options = _ydl_options(
            outtmpl=str(workdir / "%(id)s.%(ext)s"),
            postprocessors=[
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": audio_format,
                    "preferredquality": settings.ytdlp_audio_quality or "0",
                }
            ],
            ffmpeg_location=str(Path(settings.ffmpeg_path).parent),
        )
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)

        downloaded = sorted(
            (p for p in workdir.iterdir() if p.is_file() and p.suffix != ".part"),
            key=lambda p: p.stat().st_size,
            reverse=True,
        )
        if not downloaded:
            raise AcquisitionError("yt-dlp produced no output file")
        source = downloaded[0]

        # Prefer authoritative metadata when MusicBrainz is confident
        artist = item.artist_name or (info.get("artist") or info.get("uploader") or "Unknown Artist")
        title = item.title or info.get("track") or info.get("title") or "Untitled"
        album = item.album_name or info.get("album") or "Singles"
        track_no = 0
        mbid = ""

        resolved = musicbrainz.resolve_track(artist, title, item.album_name or "")
        if resolved:
            artist = resolved.get("artist") or artist
            title = resolved.get("title") or title
            album = resolved.get("album") or album
            mbid = resolved.get("recording_mbid", "")

        _tag(
            source,
            {
                "artist": artist,
                "title": title,
                "album": album,
                "albumartist": artist,
                "musicbrainz_trackid": mbid,
                "date": (resolved.get("date", "")[:4] if resolved else ""),
            },
        )

        target = _import_path(artist, album, title, track_no, source.suffix.lstrip("."))
        shutil.move(str(source), str(target))
        log.info("imported %s — %s -> %s", artist, title, target)
        return target
    finally:
        _slots.release()
        shutil.rmtree(workdir, ignore_errors=True)


# ─── Queue processing ──────────────────────────────────────────────────────


def downloads_today(db: Session) -> int:
    since = utcnow() - timedelta(days=1)
    return db.scalar(
        select(func.count(WantedItem.id)).where(
            WantedItem.provider == "ytdlp",
            WantedItem.completed_at.isnot(None),
            WantedItem.completed_at >= since,
        )
    ) or 0


def approve(db: Session, item: WantedItem) -> WantedItem:
    item.status = WantedStatus.APPROVED.value
    item.decided_at = utcnow()
    item.error_message = None
    db.add(item)
    db.commit()
    return item


def reject(db: Session, item: WantedItem) -> WantedItem:
    item.status = WantedStatus.REJECTED.value
    item.decided_at = utcnow()
    db.add(item)
    db.commit()
    return item


def auto_approve(db: Session) -> int:
    """Promote high-confidence pending items when AUTO_DOWNLOAD is on."""
    if not settings.auto_download:
        return 0

    items = db.scalars(
        select(WantedItem).where(
            WantedItem.provider == "ytdlp",
            WantedItem.status == WantedStatus.PENDING.value,
            WantedItem.confidence >= settings.acquisition_min_confidence,
        )
    ).all()
    for item in items:
        item.status = WantedStatus.APPROVED.value
        item.decided_at = utcnow()
        db.add(item)
    if items:
        db.commit()
        log.info("auto-approved %d wanted items", len(items))
    return len(items)


def process_queue(limit: int = 5) -> dict[str, int]:
    """Download approved items. Called by the scheduler."""
    stats = {"downloaded": 0, "failed": 0, "throttled": 0}
    if not settings.acquisition_enabled:
        return stats

    imported_paths: list[Path] = []

    with session_scope() as db:
        auto_approve(db)

        used = downloads_today(db)
        remaining = max(0, settings.acquisition_max_per_day - used)
        if remaining <= 0:
            log.info("daily acquisition cap reached (%d)", settings.acquisition_max_per_day)
            stats["throttled"] = 1
            return stats

        items = db.scalars(
            select(WantedItem)
            .where(
                WantedItem.provider == "ytdlp",
                WantedItem.status == WantedStatus.APPROVED.value,
            )
            .order_by(WantedItem.confidence.desc(), WantedItem.created_at.asc())
            .limit(min(limit, remaining))
        ).all()

        for item in items:
            item.status = WantedStatus.DOWNLOADING.value
            db.add(item)
            db.commit()
            try:
                path = download(item)
                item.status = WantedStatus.IMPORTED.value
                item.result_path = str(path)
                item.completed_at = utcnow()
                item.error_message = None
                imported_paths.append(path)
                stats["downloaded"] += 1
            except AcquisitionError as exc:
                item.status = WantedStatus.FAILED.value
                item.error_message = str(exc)[:500]
                stats["failed"] += 1
                log.warning("acquisition failed for %s: %s", item.artist_name, exc)
            except Exception as exc:
                item.status = WantedStatus.FAILED.value
                item.error_message = f"unexpected error: {exc}"[:500]
                stats["failed"] += 1
                log.exception("unexpected acquisition failure")
            db.add(item)
            db.commit()

    if imported_paths:
        result = scanner.scan_paths(imported_paths)
        # Link each wanted item to the track row the scan produced
        with session_scope() as db:
            for path in imported_paths:
                track = db.scalar(select(Track).where(Track.path == str(path)))
                if track is None:
                    continue
                item = db.scalar(
                    select(WantedItem).where(WantedItem.result_path == str(path))
                )
                if item is not None:
                    item.track_id = track.id
                    db.add(item)
            db.commit()
        log.info("acquisition imported %d tracks", result.added)

    return stats


def enqueue(
    db: Session,
    *,
    artist: str,
    title: str = "",
    album: str = "",
    user_id: int | None = None,
    source: str = "manual",
    confidence: float = 1.0,
    reason: str = "",
    provider: str = "ytdlp",
    status: str = WantedStatus.PENDING.value,
) -> WantedItem:
    """Add an item to the wanted queue, de-duplicating on artist/title/album."""
    existing = db.scalar(
        select(WantedItem).where(
            func.lower(WantedItem.artist_name) == artist.lower(),
            func.lower(WantedItem.title) == title.lower(),
            func.lower(WantedItem.album_name) == album.lower(),
            WantedItem.status.in_(
                [
                    WantedStatus.PENDING.value,
                    WantedStatus.APPROVED.value,
                    WantedStatus.DOWNLOADING.value,
                    WantedStatus.IMPORTED.value,
                ]
            ),
        )
    )
    if existing is not None:
        return existing

    item = WantedItem(
        user_id=user_id,
        item_type="album" if album and not title else "track",
        artist_name=artist,
        album_name=album,
        title=title,
        source=source,
        confidence=confidence,
        reason=reason,
        provider=provider,
        status=status,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item
