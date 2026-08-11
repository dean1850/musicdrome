"""MusicBrainz — canonical names, release year and recording length.

The AI writes track names from memory, so it produces things like "Fleetwood
Mac - The Chain (Remastered)" when the recording is credited "The Chain". Every
suggestion is resolved here before it is stored, which does three jobs at once:
it fixes the display text, it gives the downloader an authoritative duration to
match candidates against, and it lets the exclusion check run against the
canonical spelling rather than the one the model happened to use.

The public server asks for no more than one request per second from a single
client, and enforces it with 503s. :func:`_throttle` honours that.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx

from .. import config

log = logging.getLogger(__name__)

TIMEOUT = 20.0
MIN_INTERVAL = 1.05  # seconds between requests, per the MusicBrainz guidelines

_lock = threading.Lock()
_last_request = 0.0


class MusicBrainzError(RuntimeError):
    pass


def _throttle() -> None:
    global _last_request
    with _lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


def _get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    _throttle()
    url = f"{config.MUSICBRAINZ_API_URL.rstrip('/')}/{path.lstrip('/')}"
    headers = {"User-Agent": config.MUSICBRAINZ_USER_AGENT, "Accept": "application/json"}
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.get(url, params={**params, "fmt": "json"}, headers=headers)
    except httpx.HTTPError as exc:
        raise MusicBrainzError(f"network error: {exc}") from exc

    if response.status_code >= 400:
        raise MusicBrainzError(f"HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise MusicBrainzError("invalid response") from exc


def _lucene_escape(value: str) -> str:
    for char in '+-&|!(){}[]^"~*?:\\/':
        value = value.replace(char, f"\\{char}")
    return value


def _credit(recording: dict) -> str:
    parts = [
        (credit.get("artist") or {}).get("name", "") + (credit.get("joinphrase") or "")
        for credit in recording.get("artist-credit") or []
    ]
    return "".join(parts).strip()


def _earliest_release(recording: dict) -> dict[str, str]:
    """The oldest release carrying this recording — usually the original album.

    Preferring an album over a single or compilation keeps downloads filed under
    the record people actually know, rather than "Now That's What I Call 1997".
    """
    best: dict[str, str] = {"album": "", "year": "", "release_mbid": "", "track": 0}
    best_date = "9999"
    for release in recording.get("releases") or []:
        date = str(release.get("date") or "")
        group = release.get("release-group") or {}
        primary = (group.get("primary-type") or "").lower()
        secondary = [s.lower() for s in group.get("secondary-types") or []]
        if "compilation" in secondary and best["album"]:
            continue
        if not date or date < best_date or (primary == "album" and not best["album"]):
            media = (release.get("media") or [{}])[0]
            track_no = ((media.get("tracks") or [{}])[0]).get("position") or 0
            best = {
                "album": release.get("title", ""),
                "year": date[:4],
                "release_mbid": release.get("id", ""),
                "track": int(track_no or 0),
            }
            if date:
                best_date = date
    return best


def resolve_track(artist: str, title: str, album: str = "") -> dict[str, Any] | None:
    """Canonical metadata for a recording, or ``None`` when nothing matches.

    MusicBrainz scores its own results 0-100; anything under 80 is usually a
    different song by a similarly named artist, so it is discarded rather than
    used to overwrite what the AI gave us.
    """
    if not config.MUSICBRAINZ_API_URL:
        return None

    query = f'recording:"{_lucene_escape(title)}" AND artist:"{_lucene_escape(artist)}"'
    if album:
        query += f' AND release:"{_lucene_escape(album)}"'

    try:
        data = _get("recording", {"query": query, "limit": 5})
    except MusicBrainzError as exc:
        log.debug("musicbrainz lookup failed for %s - %s: %s", artist, title, exc)
        return None

    recordings = data.get("recordings") or []
    if not recordings:
        return None

    best = recordings[0]
    if int(best.get("score") or 0) < 80:
        return None

    release = _earliest_release(best)
    return {
        "artist": _credit(best) or artist,
        "title": best.get("title") or title,
        "album": release["album"] or album,
        "year": release["year"],
        "track": release["track"],
        "duration": int(best.get("length") or 0) // 1000,
        "recording_mbid": best.get("id", ""),
        "release_mbid": release["release_mbid"],
    }


def cover_url(release_mbid: str) -> str:
    """A Cover Art Archive front-cover URL. Not fetched here — just built."""
    return f"https://coverartarchive.org/release/{release_mbid}/front-500" if release_mbid else ""
