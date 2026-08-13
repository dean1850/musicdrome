"""Turning a pasted link into something downloadable.

Three kinds of link are accepted, and they resolve in two different ways.

A **YouTube or YouTube Music** link already identifies the exact recording, so
it is handed straight to the downloader with no matching step — second-guessing
a link the user chose deliberately would be wrong.

A **Spotify** link identifies a recording we cannot download, so it is used for
its metadata only: the public ``open.spotify.com/embed`` page carries the
artist, title and album as JSON, which then goes through the normal YouTube
Music match. No Spotify account, API key or client secret is involved — the
embed endpoint is what Spotify serves to any website embedding a player.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

log = logging.getLogger(__name__)

TIMEOUT = 20.0

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
                 "music.youtube.com", "youtu.be", "www.youtu.be"}
SPOTIFY_HOSTS = {"open.spotify.com", "spotify.com", "www.spotify.com", "play.spotify.com"}

_NEXT_DATA = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_SPOTIFY_ID = re.compile(r"^[A-Za-z0-9]{22}$")


class LinkError(RuntimeError):
    pass


# ─── YouTube ───────────────────────────────────────────────────────────────


def youtube_video_id(url: str) -> str | None:
    """The 11-character video id in a YouTube or YouTube Music URL."""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        return None

    if host.endswith("youtu.be"):
        candidate = parsed.path.lstrip("/").split("/")[0]
    elif parsed.path.startswith(("/shorts/", "/embed/", "/v/")):
        candidate = parsed.path.split("/")[2] if len(parsed.path.split("/")) > 2 else ""
    else:
        candidate = (parse_qs(parsed.query).get("v") or [""])[0]

    return candidate if _VIDEO_ID.match(candidate or "") else None


def youtube_metadata(video_id: str) -> dict[str, Any]:
    """Artist and title for a video, so the download can be named and tagged.

    yt-dlp's own metadata is used rather than the page title: on YouTube Music
    and on "- Topic" channels it carries properly separated artist and track
    fields, which a title string does not.
    """
    import yt_dlp

    from .download import _extract_info, _ydl_options

    url = f"https://music.youtube.com/watch?v={video_id}"
    try:
        info = _extract_info(yt_dlp, _ydl_options(skip_download=True), url, download=False) or {}
    except Exception as exc:
        raise LinkError(f"could not read that YouTube link: {exc}") from exc

    title = (info.get("track") or info.get("title") or "").strip()
    artist = (info.get("artist") or info.get("creator") or info.get("uploader") or "").strip()
    # Uploader names on auto-generated artist channels end in " - Topic".
    artist = re.sub(r"\s*-\s*Topic$", "", artist).strip()

    # "Artist - Title" in the video title, when the fields were not separate.
    if not info.get("artist") and " - " in title:
        left, right = title.split(" - ", 1)
        artist, title = left.strip(), right.strip()

    if not title:
        raise LinkError("that YouTube link has no readable title")

    return {
        "artist": artist or "Unknown Artist",
        "title": title,
        "album": (info.get("album") or "").strip(),
        "duration": int(info.get("duration") or 0),
        "url": url,
        "source": "ytmusic" if info.get("artist") else "youtube",
    }


# ─── Spotify ───────────────────────────────────────────────────────────────


def spotify_track_id(url: str) -> str | None:
    """The track id in a Spotify URL, whether a link or a ``spotify:`` URI."""
    if url.startswith("spotify:track:"):
        candidate = url.split(":")[2].split("?")[0]
        return candidate if _SPOTIFY_ID.match(candidate) else None

    parsed = urlparse(url if "://" in url else f"https://{url}")
    if (parsed.hostname or "").lower() not in SPOTIFY_HOSTS:
        return None

    parts = [p for p in parsed.path.split("/") if p]
    # Locale-prefixed links look like /intl-pt/track/<id>.
    if "track" not in parts:
        return None
    index = parts.index("track")
    candidate = parts[index + 1] if len(parts) > index + 1 else ""
    return candidate if _SPOTIFY_ID.match(candidate) else None


def _entity(payload: dict[str, Any]) -> dict[str, Any]:
    """Dig the track entity out of the embed page's Next.js state.

    The exact nesting has moved between Spotify's own releases, so this walks
    the likely paths and then falls back to a search for any object that looks
    like a track rather than hard-coding one shape.
    """
    for path in (
        ("props", "pageProps", "state", "data", "entity"),
        ("props", "pageProps", "state", "data", "track"),
        ("props", "pageProps", "track"),
    ):
        node: Any = payload
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict) and node.get("name"):
            return node

    stack: list[Any] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("name") and ("artists" in node or "subtitle" in node):
                return node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return {}


def spotify_metadata(track_id: str) -> dict[str, Any]:
    """Artist, title and album for a Spotify track, from its public embed page."""
    url = f"https://open.spotify.com/embed/track/{track_id}"
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
            response = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
    except httpx.HTTPError as exc:
        raise LinkError(f"could not reach Spotify: {exc}") from exc

    if response.status_code == 404:
        raise LinkError("Spotify does not have a track with that id")
    if response.status_code >= 400:
        raise LinkError(f"Spotify returned HTTP {response.status_code}")

    match = _NEXT_DATA.search(response.text)
    if not match:
        raise LinkError("Spotify's embed page was not in the expected format")

    try:
        entity = _entity(json.loads(match.group(1)))
    except ValueError as exc:
        raise LinkError("could not parse Spotify's embed data") from exc

    title = (entity.get("name") or "").strip()
    if not title:
        raise LinkError("that Spotify link has no readable track name")

    artists = entity.get("artists") or []
    artist = ""
    if isinstance(artists, list) and artists:
        first = artists[0]
        artist = (first.get("name") if isinstance(first, dict) else str(first)) or ""
    if not artist:
        artist = (entity.get("subtitle") or "").split(",")[0]

    duration = entity.get("duration") or entity.get("duration_ms") or 0
    return {
        "artist": artist.strip() or "Unknown Artist",
        "title": title,
        "album": ((entity.get("album") or {}).get("name") or "").strip()
        if isinstance(entity.get("album"), dict) else "",
        "duration": int(duration) // 1000 if int(duration or 0) > 10_000 else int(duration or 0),
        "url": "",  # Spotify audio is not downloadable — match on YouTube Music
        "source": "spotify",
    }


# ─── Entry point ───────────────────────────────────────────────────────────


def resolve(url: str) -> dict[str, Any]:
    """Resolve any supported link into artist, title and (maybe) a direct URL.

    An empty ``url`` in the result means "search for this" rather than "fetch
    this" — the caller hands it to the normal matching path.
    """
    url = (url or "").strip()
    if not url:
        raise LinkError("no link given")

    video_id = youtube_video_id(url)
    if video_id:
        return youtube_metadata(video_id)

    track_id = spotify_track_id(url)
    if track_id:
        return spotify_metadata(track_id)

    if "spotify.com" in url and "/track/" not in url:
        raise LinkError(
            "that is a Spotify album, playlist or artist link — paste a track link"
        )
    raise LinkError(
        "unrecognised link — paste a Spotify track, YouTube Music or YouTube URL"
    )
