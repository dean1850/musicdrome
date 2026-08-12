"""Last.fm — listening history and track metadata.

Read methods only need an API key, which is why Musicdrome never asks for your
password or an API secret: it reads ``user.getRecentTracks`` from a public
profile and looks up tags and cover art. There is no write path.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

import httpx

from .. import config

log = logging.getLogger(__name__)

API_ROOT = "https://ws.audioscrobbler.com/2.0/"
TIMEOUT = 20.0
PAGE_SIZE = 200


class LastFmError(RuntimeError):
    pass


def configured(user: str = "") -> bool:
    """Whether history can be read for ``user``, or for the environment's user.

    The API key is shared by the whole household — it authenticates Musicdrome,
    not a person — so only the username varies per user.
    """
    return bool(config.LASTFM_API_KEY and (user or config.LASTFM_USER))


def _get(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if not config.LASTFM_API_KEY:
        raise LastFmError("LASTFM_API_KEY is not set")

    payload = {k: v for k, v in params.items() if v not in (None, "")}
    payload.update(method=method, api_key=config.LASTFM_API_KEY, format="json")

    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.get(API_ROOT, params=payload)
    except httpx.HTTPError as exc:
        raise LastFmError(f"network error: {exc}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise LastFmError(f"invalid response: {response.text[:200]}") from exc

    if isinstance(data, dict) and "error" in data:
        raise LastFmError(f"Last.fm error {data.get('error')}: {data.get('message', '')}")
    return data


def _largest_image(images: list[dict] | None) -> str:
    """Last.fm orders images small to extralarge; take the biggest with a URL."""
    for image in reversed(images or []):
        url = (image or {}).get("#text", "")
        if url:
            return url
    return ""


# ─── Listening history ─────────────────────────────────────────────────────


def recent_tracks(since: int = 0, max_pages: int = 25, user: str = "") -> Iterator[dict[str, Any]]:
    """Yield plays newer than ``since`` (unix seconds), oldest page last.

    ``user`` names whose profile to read, falling back to the environment's so
    a single-user install keeps working with no user rows at all.

    ``max_pages`` caps a first sync against a very old profile so the initial
    boot cannot spend ten minutes paging; the next scan picks up where this one
    stopped, because the cursor only advances over what was actually read.
    """
    page = 1
    while page <= max_pages:
        data = _get(
            "user.getRecentTracks",
            {
                "user": user or config.LASTFM_USER,
                "limit": PAGE_SIZE,
                "page": page,
                # `from` is a lower bound on the scrobble timestamp. Passing the
                # cursor itself rather than cursor+1 can re-fetch the boundary
                # play, which the UNIQUE constraint absorbs — the other way
                # round would silently skip it.
                "from": since or None,
                "extended": 0,
            },
        )
        block = data.get("recenttracks") or {}
        entries = block.get("track") or []
        if isinstance(entries, dict):
            entries = [entries]

        for entry in entries:
            attr = entry.get("@attr") or {}
            if attr.get("nowplaying") == "true":
                continue  # not a completed play yet
            played_at = int(((entry.get("date") or {}).get("uts") or 0))
            if not played_at:
                continue
            artist = ((entry.get("artist") or {}).get("#text") or "").strip()
            title = (entry.get("name") or "").strip()
            if not artist or not title:
                continue
            yield {
                "artist": artist,
                "title": title,
                "album": ((entry.get("album") or {}).get("#text") or "").strip(),
                "played_at": played_at,
                "source": "lastfm",
            }

        total_pages = int((block.get("@attr") or {}).get("totalPages") or 1)
        if page >= total_pages:
            return
        page += 1


# ─── Metadata ──────────────────────────────────────────────────────────────


def track_info(artist: str, title: str) -> dict[str, Any] | None:
    """Tags, cover art and popularity for one track, or ``None``."""
    try:
        data = _get("track.getInfo", {"artist": artist, "track": title, "autocorrect": 1})
    except LastFmError as exc:
        log.debug("track.getInfo failed for %s - %s: %s", artist, title, exc)
        return None

    info = data.get("track") or {}
    if not info:
        return None

    album = info.get("album") or {}
    tags = [t.get("name", "") for t in (info.get("toptags") or {}).get("tag", []) or []]
    return {
        "artist": (info.get("artist") or {}).get("name", artist),
        "title": info.get("name", title),
        "album": album.get("title", ""),
        "cover_url": _largest_image(album.get("image")),
        "tags": [t for t in tags if t][:5],
        "listeners": int(info.get("listeners") or 0),
        "duration": int(info.get("duration") or 0) // 1000,
    }


def artist_tags(artist: str) -> list[str]:
    """Genre tags for an artist — used when a track has none of its own."""
    try:
        data = _get("artist.getInfo", {"artist": artist, "autocorrect": 1})
    except LastFmError:
        return []
    tags = ((data.get("artist") or {}).get("tags") or {}).get("tag", []) or []
    return [t.get("name", "") for t in tags if t.get("name")][:5]
