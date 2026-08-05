"""Subsonic media delivery: stream, download and cover art.

These are the only Subsonic endpoints that do not return the standard envelope —
they return the bytes themselves. Errors still come back as a Subsonic error
document so clients can display something meaningful.
"""

from __future__ import annotations

import hashlib
import io
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Album, Artist, Playlist, PodcastEpisode, Track
from ..services import transcode
from ..services.scrobble import submit_now_playing
from .common import (
    ALBUM,
    ARTIST,
    EPISODE,
    PLAYLIST,
    TRACK,
    SubsonicContext,
    SubsonicError,
    endpoint,
    get_context,
    now_playing,
    param_bool,
    param_int,
    params_of,
    parse_id,
)

log = logging.getLogger(__name__)

router = APIRouter()


def _resolve_media(ctx: SubsonicContext, raw_id: str | None) -> tuple[Path, Track | None, PodcastEpisode | None]:
    """Resolve a stream/download id to a file on disk."""
    prefix, item_id = parse_id(raw_id, TRACK)

    if prefix == EPISODE:
        episode = ctx.db.get(PodcastEpisode, item_id)
        if episode is None:
            raise SubsonicError(SubsonicError.NOT_FOUND, "Episode not found")
        if not episode.path:
            raise SubsonicError(
                SubsonicError.NOT_FOUND,
                "Episode has not been downloaded yet — call downloadPodcastEpisode first",
            )
        path = Path(episode.path)
        if not path.exists():
            raise SubsonicError(SubsonicError.NOT_FOUND, "Episode file is missing from disk")
        return path, None, episode

    track = ctx.db.get(Track, item_id)
    if track is None:
        raise SubsonicError(SubsonicError.NOT_FOUND, "Song not found")

    path = Path(track.path)
    if not path.exists():
        raise SubsonicError(SubsonicError.NOT_FOUND, "Media file is missing from disk")
    return path, track, None


@endpoint(router, "stream")
def stream(request: Request, ctx: SubsonicContext = Depends(get_context)):
    if not ctx.user.stream_role:
        raise SubsonicError(
            SubsonicError.NOT_AUTHORIZED, "User is not authorized to stream"
        )

    params = params_of(request)
    path, track, episode = _resolve_media(ctx, params.get("id"))

    requested_format = params.get("format")
    max_bitrate = param_int(params, "maxBitRate", 0) or 0
    offset = param_int(params, "timeOffset", 0) or 0
    estimate_length = param_bool(params, "estimateContentLength", False)

    if episode is not None:
        # Podcast episodes are served as-is; transcoding them adds nothing.
        return FileResponse(
            path,
            media_type=episode.content_type or "audio/mpeg",
            headers={"Accept-Ranges": "bytes"},
        )

    assert track is not None
    plan = transcode.plan_stream(
        track,
        ctx.user,
        requested_format=requested_format,
        requested_bitrate=max_bitrate or None,
    )

    now_playing.update(ctx.user, track, ctx.client)
    try:
        submit_now_playing(ctx.user, track)
    except Exception:
        log.debug("now-playing submission failed", exc_info=True)

    if not plan.transcode:
        file_size = path.stat().st_size
        byte_range = transcode.parse_range_header(
            request.headers.get("range"), file_size
        )
        if byte_range:
            start, end = byte_range
            return StreamingResponse(
                transcode.stream_direct(path, start, end),
                status_code=206,
                media_type=plan.content_type,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Content-Length": str(end - start + 1),
                    "Accept-Ranges": "bytes",
                },
            )
        return FileResponse(
            path,
            media_type=plan.content_type,
            headers={"Accept-Ranges": "bytes"},
        )

    headers = {
        "Accept-Ranges": "none",
        "X-Content-Duration": str(track.duration),
    }
    if estimate_length and plan.estimated_size:
        headers["Content-Length"] = str(plan.estimated_size)

    try:
        generator = transcode.stream_transcoded(plan, offset=offset)
    except RuntimeError as exc:
        raise SubsonicError(SubsonicError.GENERIC, str(exc))

    return StreamingResponse(generator, media_type=plan.content_type, headers=headers)


@endpoint(router, "download")
def download(request: Request, ctx: SubsonicContext = Depends(get_context)):
    if not ctx.user.download_role:
        raise SubsonicError(
            SubsonicError.NOT_AUTHORIZED, "User is not authorized to download"
        )

    params = params_of(request)
    path, track, episode = _resolve_media(ctx, params.get("id"))
    media_type = (
        track.content_type if track else (episode.content_type if episode else None)
    )
    return FileResponse(
        path,
        media_type=media_type or "application/octet-stream",
        filename=path.name,
    )


@endpoint(router, "hls")
def hls(request: Request, ctx: SubsonicContext = Depends(get_context)):
    # HLS segmenting is not implemented; clients fall back to progressive
    # streaming when this returns an error.
    raise SubsonicError(
        SubsonicError.NOT_AUTHORIZED, "HLS is not supported by this server — use stream"
    )


# ─── Cover art ─────────────────────────────────────────────────────────────


def _cover_path(db: Session, raw_id: str | None) -> Path | None:
    """Resolve a cover-art id to a file. Shared with the native API."""
    prefix, item_id = parse_id(raw_id, ALBUM)

    candidate: str | None = None
    if prefix == ALBUM:
        album = db.get(Album, item_id)
        candidate = album.cover_art_path if album else None
    elif prefix == ARTIST:
        artist = db.get(Artist, item_id)
        candidate = artist.image_path if artist else None
    elif prefix == TRACK:
        track = db.get(Track, item_id)
        if track:
            candidate = track.cover_art_path
            if not candidate and track.album_id:
                album = db.get(Album, track.album_id)
                candidate = album.cover_art_path if album else None
    elif prefix == PLAYLIST:
        playlist = db.get(Playlist, item_id)
        candidate = playlist.cover_art_path if playlist else None
    elif prefix == EPISODE:
        episode = db.get(PodcastEpisode, item_id)
        if episode and episode.channel:
            candidate = episode.channel.image_path

    if not candidate:
        return None
    path = Path(candidate)
    return path if path.exists() else None


def _placeholder_cover(seed: str, size: int) -> bytes:
    """Deterministic gradient placeholder so the UI never shows a broken image."""
    from PIL import Image, ImageDraw

    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    top = (digest[0] // 2 + 30, digest[1] // 2 + 30, digest[2] // 2 + 40)
    bottom = (digest[3] // 3 + 15, digest[4] // 3 + 15, digest[5] // 3 + 25)

    image = Image.new("RGB", (size, size), top)
    draw = ImageDraw.Draw(image)
    for y in range(size):
        blend = y / max(size - 1, 1)
        draw.line(
            [(0, y), (size, y)],
            fill=tuple(
                int(top[i] * (1 - blend) + bottom[i] * blend) for i in range(3)
            ),
        )

    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    return buffer.getvalue()


@endpoint(router, "getCoverArt")
def get_cover_art(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    raw_id = params.get("id") or ""
    size = param_int(params, "size", 0) or 0

    path = _cover_path(ctx.db, raw_id)

    if path is None:
        image = _placeholder_cover(raw_id or "musicdrome", size or 300)
        return Response(
            content=image,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    if not size:
        return FileResponse(
            path, headers={"Cache-Control": "public, max-age=604800"}
        )

    size = max(32, min(size, 2000))
    cache_name = f"{hashlib.sha1(f'{path}:{size}'.encode()).hexdigest()}.jpg"
    cached = settings.cache_dir / "covers" / cache_name
    cached.parent.mkdir(parents=True, exist_ok=True)

    if not cached.exists():
        try:
            from PIL import Image

            with Image.open(path) as image:
                if image.mode not in ("RGB", "L"):
                    image = image.convert("RGB")
                image.thumbnail((size, size), Image.LANCZOS)
                image.save(cached, "JPEG", quality=88)
        except Exception as exc:
            log.debug("cover resize failed for %s: %s", path, exc)
            return FileResponse(path)

    return FileResponse(cached, headers={"Cache-Control": "public, max-age=604800"})


@endpoint(router, "getAvatar")
def get_avatar(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    username = params.get("username") or ctx.user.username
    return Response(
        content=_placeholder_cover(f"avatar:{username}", 160),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )
