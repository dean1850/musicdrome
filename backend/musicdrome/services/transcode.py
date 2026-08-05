"""Streaming and on-the-fly transcoding.

A stream is only re-encoded when it has to be: the client asked for a different
container, or the source bitrate exceeds the ceiling that applies to this user.
Otherwise the original file is served directly, with byte-range support so
clients can seek.

Finished transcodes are cached on disk keyed by ``(path, mtime, format,
bitrate)``, so the second play of a track costs nothing. The cache is trimmed to
``TRANSCODE_CACHE_SIZE_MB`` least-recently-used first.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ..config import settings
from ..models import Track, User

log = logging.getLogger(__name__)

CHUNK_SIZE = 64 * 1024

# Bound the number of concurrent ffmpeg processes so a busy server does not
# thrash. Acquired for the lifetime of a transcoding response.
_transcode_slots = threading.Semaphore(settings.transcode_max_concurrent)

FORMAT_CONTENT_TYPES = {
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "ogg": "audio/ogg",
    "vorbis": "audio/ogg",
    "aac": "audio/aac",
    "m4a": "audio/mp4",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "raw": "audio/mpeg",
}

# ffmpeg arguments per target format. ``{bitrate}`` is substituted with kbps.
FORMAT_ENCODERS: dict[str, list[str]] = {
    "mp3": ["-c:a", "libmp3lame", "-b:a", "{bitrate}k", "-f", "mp3"],
    "opus": ["-c:a", "libopus", "-b:a", "{bitrate}k", "-vbr", "on", "-f", "ogg"],
    "ogg": ["-c:a", "libvorbis", "-b:a", "{bitrate}k", "-f", "ogg"],
    "vorbis": ["-c:a", "libvorbis", "-b:a", "{bitrate}k", "-f", "ogg"],
    "aac": ["-c:a", "aac", "-b:a", "{bitrate}k", "-f", "adts"],
    "m4a": ["-c:a", "aac", "-b:a", "{bitrate}k", "-f", "ipod", "-movflags", "frag_keyframe+empty_moov"],
    "flac": ["-c:a", "flac", "-f", "flac"],
    "wav": ["-c:a", "pcm_s16le", "-f", "wav"],
}

LOSSLESS_SUFFIXES = {"flac", "wav", "aiff", "aif", "ape", "wv"}


_ffmpeg_warned = False


def ffmpeg_available() -> bool:
    return (
        shutil.which(settings.ffmpeg_path) is not None
        or Path(settings.ffmpeg_path).exists()
    )


def _warn_missing_ffmpeg() -> None:
    global _ffmpeg_warned
    if not _ffmpeg_warned:
        _ffmpeg_warned = True
        log.warning(
            "ffmpeg not found at %s — transcoding is unavailable and streams "
            "will be served in their original format. Set FFMPEG_PATH in .env.",
            settings.ffmpeg_path,
        )


@dataclass
class StreamPlan:
    """What we are actually going to send for one stream request."""

    transcode: bool
    fmt: str
    bitrate: int
    content_type: str
    source: Path
    estimated_size: int = 0

    @property
    def suffix(self) -> str:
        return "mp3" if self.fmt == "raw" else self.fmt


# ─── Planning ──────────────────────────────────────────────────────────────


def effective_max_bitrate(user: User | None, requested: int | None) -> int:
    """Lowest of: what the client asked for, the user's cap, the server cap."""
    limits = [
        value
        for value in (
            requested or 0,
            (user.max_bitrate if user else 0) or 0,
            settings.default_max_bitrate or 0,
        )
        if value > 0
    ]
    return min(limits) if limits else 0


def plan_stream(
    track: Track,
    user: User | None = None,
    *,
    requested_format: str | None = None,
    requested_bitrate: int | None = None,
) -> StreamPlan:
    source = Path(track.path)
    source_format = (track.suffix or source.suffix.lstrip(".")).lower()
    max_bitrate = effective_max_bitrate(user, requested_bitrate)

    fmt = (requested_format or "").lower().strip()

    # Without ffmpeg there is nothing to transcode *with*. Serving the original
    # beats failing the request — the client gets playable audio, just not in
    # the container it asked for. The failure is logged once per process.
    if fmt != "raw" and settings.transcoding_enabled and not ffmpeg_available():
        _warn_missing_ffmpeg()
        fmt = "raw"

    if fmt == "raw" or not settings.transcoding_enabled:
        # 'raw' is the Subsonic escape hatch meaning "do not touch this stream".
        return StreamPlan(
            transcode=False,
            fmt=source_format,
            bitrate=track.bitrate,
            content_type=track.content_type or "audio/mpeg",
            source=source,
            estimated_size=track.size,
        )

    if not fmt:
        # No explicit request: only step in if the bitrate ceiling demands it.
        needs_downsample = bool(
            max_bitrate and track.bitrate and track.bitrate > max_bitrate
        )
        needs_downsample = needs_downsample or (
            bool(max_bitrate) and source_format in LOSSLESS_SUFFIXES
        )
        if not needs_downsample:
            return StreamPlan(
                transcode=False,
                fmt=source_format,
                bitrate=track.bitrate,
                content_type=track.content_type or "audio/mpeg",
                source=source,
                estimated_size=track.size,
            )
        fmt = settings.default_transcode_format

    if fmt not in FORMAT_ENCODERS:
        log.debug("unsupported target format %r, falling back to %s",
                  fmt, settings.default_transcode_format)
        fmt = settings.default_transcode_format

    same_container = fmt == source_format
    within_budget = not max_bitrate or (track.bitrate and track.bitrate <= max_bitrate)
    if same_container and within_budget:
        return StreamPlan(
            transcode=False,
            fmt=source_format,
            bitrate=track.bitrate,
            content_type=track.content_type or "audio/mpeg",
            source=source,
            estimated_size=track.size,
        )

    bitrate = max_bitrate or settings.default_max_bitrate or 192
    if track.bitrate and track.bitrate < bitrate and source_format not in LOSSLESS_SUFFIXES:
        # Never "upscale" a low-bitrate source — it only wastes bandwidth.
        bitrate = track.bitrate

    return StreamPlan(
        transcode=True,
        fmt=fmt,
        bitrate=bitrate,
        content_type=FORMAT_CONTENT_TYPES.get(fmt, "audio/mpeg"),
        source=source,
        estimated_size=int(track.duration * bitrate * 1000 / 8) if track.duration else 0,
    )


# ─── Cache ─────────────────────────────────────────────────────────────────


def _cache_key(plan: StreamPlan) -> str:
    try:
        mtime = plan.source.stat().st_mtime
    except OSError:
        mtime = 0.0
    raw = f"{plan.source}|{mtime}|{plan.fmt}|{plan.bitrate}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _cache_path(plan: StreamPlan) -> Path:
    return settings.transcode_cache_dir / f"{_cache_key(plan)}.{plan.suffix}"


def prune_cache() -> int:
    """Trim the transcode cache to its configured size, LRU first."""
    limit_bytes = settings.transcode_cache_size_mb * 1024 * 1024
    if limit_bytes <= 0:
        return 0

    cache_dir = settings.transcode_cache_dir
    if not cache_dir.exists():
        return 0

    entries: list[tuple[float, int, Path]] = []
    total = 0
    for path in cache_dir.iterdir():
        if not path.is_file() or path.suffix == ".part":
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((stat.st_atime, stat.st_size, path))
        total += stat.st_size

    if total <= limit_bytes:
        return 0

    entries.sort(key=lambda item: item[0])  # oldest access first
    removed = 0
    for _atime, size, path in entries:
        if total <= limit_bytes:
            break
        try:
            path.unlink()
            total -= size
            removed += 1
        except OSError:
            continue

    if removed:
        log.info("pruned %d cached transcodes", removed)
    return removed


# ─── ffmpeg ────────────────────────────────────────────────────────────────


def _ffmpeg_command(plan: StreamPlan, *, offset: int = 0, output: str = "pipe:1") -> list[str]:
    args = [settings.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if offset > 0:
        args += ["-ss", str(offset)]
    args += ["-i", str(plan.source), "-vn", "-map_metadata", "0"]
    encoder = FORMAT_ENCODERS.get(plan.fmt, FORMAT_ENCODERS["mp3"])
    args += [part.format(bitrate=plan.bitrate) for part in encoder]
    args += ["-y", output]
    return args


def _iter_file(path: Path, start: int = 0, end: int | None = None) -> Iterator[bytes]:
    """Yield a byte range from a file."""
    with path.open("rb") as handle:
        if start:
            handle.seek(start)
        remaining = None if end is None else max(0, end - start + 1)
        while True:
            size = CHUNK_SIZE if remaining is None else min(CHUNK_SIZE, remaining)
            if size <= 0:
                break
            chunk = handle.read(size)
            if not chunk:
                break
            if remaining is not None:
                remaining -= len(chunk)
            yield chunk


def stream_direct(path: Path, start: int = 0, end: int | None = None) -> Iterator[bytes]:
    return _iter_file(path, start, end)


def stream_transcoded(plan: StreamPlan, *, offset: int = 0) -> Iterator[bytes]:
    """Yield transcoded audio, populating the cache as a side effect.

    Seeked requests (``offset > 0``) bypass the cache — the output would be a
    partial file and caching it would poison later full plays.
    """
    cache_path = _cache_path(plan)
    use_cache = settings.transcode_cache_enabled and offset == 0

    if use_cache and cache_path.exists():
        try:
            os.utime(cache_path, None)  # refresh atime for LRU
        except OSError:
            pass
        log.debug("serving cached transcode %s", cache_path.name)
        yield from _iter_file(cache_path)
        return

    if not ffmpeg_available():
        raise RuntimeError(
            f"ffmpeg not found at {settings.ffmpeg_path}; set FFMPEG_PATH in .env"
        )

    partial = cache_path.with_suffix(cache_path.suffix + ".part") if use_cache else None
    command = _ffmpeg_command(plan, offset=offset)
    log.debug("transcoding: %s", " ".join(command))

    acquired = _transcode_slots.acquire(timeout=30)
    if not acquired:
        raise RuntimeError("server is busy transcoding; try again shortly")

    process = None
    sink = None
    completed = False
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=CHUNK_SIZE
        )
        if partial is not None:
            try:
                sink = partial.open("wb")
            except OSError:
                sink = None

        assert process.stdout is not None
        while True:
            chunk = process.stdout.read(CHUNK_SIZE)
            if not chunk:
                break
            if sink is not None:
                sink.write(chunk)
            yield chunk

        process.wait(timeout=30)
        completed = process.returncode == 0
        if not completed:
            stderr = b""
            if process.stderr is not None:
                stderr = process.stderr.read() or b""
            log.warning(
                "ffmpeg exited %s for %s: %s",
                process.returncode, plan.source, stderr.decode("utf-8", "replace")[:400],
            )
    except GeneratorExit:
        # Client hung up mid-stream — normal when a user skips a track.
        completed = False
        raise
    finally:
        if sink is not None:
            sink.close()
        if partial is not None:
            if completed:
                try:
                    partial.replace(cache_path)
                except OSError:
                    partial.unlink(missing_ok=True)
            else:
                partial.unlink(missing_ok=True)
        if process is not None and process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if process is not None:
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass
        _transcode_slots.release()


def parse_range_header(value: str | None, file_size: int) -> tuple[int, int] | None:
    """Parse ``Range: bytes=start-end``. Returns ``None`` when absent/invalid."""
    if not value or not value.startswith("bytes="):
        return None
    spec = value[6:].split(",")[0].strip()
    if "-" not in spec:
        return None
    start_text, _, end_text = spec.partition("-")
    try:
        if start_text:
            start = int(start_text)
            end = int(end_text) if end_text else file_size - 1
        else:
            # Suffix form: bytes=-500 means "the last 500 bytes"
            if not end_text:
                return None
            length = int(end_text)
            start = max(0, file_size - length)
            end = file_size - 1
    except ValueError:
        return None

    if start < 0 or start >= file_size:
        return None
    end = min(end, file_size - 1)
    if end < start:
        return None
    return start, end
