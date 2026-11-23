"""MusicBrainz — canonical names, release year and recording length.

The AI writes track names from memory, so it produces things like "Fleetwood
Mac - The Chain (Remastered)" when the recording is credited "The Chain". Every
suggestion is resolved here before it is stored, which does three jobs at once:
it fixes the display text, it gives the downloader an authoritative duration to
match candidates against, and it lets the exclusion check run against the
canonical spelling rather than the one the model happened to use.

The public server asks for no more than one request per second from a single
client, and enforces it with 503s. :func:`_throttle` honours that.

It is also where genres come from when there is no Last.fm key —
:func:`artist_genres`, below — so dropping Last.fm costs a card its tags only
when MusicBrainz has none to give either.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx

from .. import config, net

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
    def fetch() -> httpx.Response:
        with httpx.Client(timeout=TIMEOUT) as client:
            return client.get(url, params={**params, "fmt": "json"}, headers=headers)

    try:
        response = net.with_retry(fetch, what=f"musicbrainz {path}")
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


def _lead_artist_id(recording: dict) -> str:
    """The MBID of the first credited artist — the one a track is filed under."""
    for credit in recording.get("artist-credit") or []:
        artist_id = (credit.get("artist") or {}).get("id") if isinstance(credit, dict) else ""
        if artist_id:
            return str(artist_id)
    return ""


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
        "artist_mbid": _lead_artist_id(best),
    }


def cover_url(release_mbid: str) -> str:
    """A Cover Art Archive front-cover URL. Not fetched here — just built."""
    return f"https://coverartarchive.org/release/{release_mbid}/front-500" if release_mbid else ""


# ─── Genres ────────────────────────────────────────────────────────────────

# As many as Last.fm's tags give a card, so the genre filter looks the same
# whichever of the two filled it in.
GENRES_PER_ARTIST = 5

# Artist MBID → genres, for the life of the process. A scan suggests up to two
# tracks per artist, and the next scan suggests many of the same neighbours, so
# without this every repeat is another second of throttled waiting for an answer
# that has not changed. Only answers are kept: a lookup that failed is retried
# next time rather than remembered as "no genres".
_genre_cache: dict[str, list[str]] = {}
GENRE_CACHE_LIMIT = 5000


def artist_genres(artist_mbid: str) -> list[str]:
    """An artist's genres, most-voted first, or ``[]`` if there are none.

    Only used when Last.fm is not configured. Anyone with a key keeps Last.fm's
    tags and never waits on this; anyone without one pays a throttled second
    per artist, once, in exchange for cards that still have genres.

    The artist's genres rather than the recording's, for the same reason the
    Last.fm path falls back to ``artist.getInfo``: almost nobody tags a single
    recording, while artists are tagged constantly. And *genres* rather than
    tags — MusicBrainz's genres are the tags that appear on its curated genre
    list, so a card gets "trip hop" and never "seen live" or "favourites".
    """
    if not artist_mbid or not config.MUSICBRAINZ_API_URL:
        return []

    cached = _genre_cache.get(artist_mbid)
    if cached is not None:
        return list(cached)

    try:
        data = _get(f"artist/{artist_mbid}", {"inc": "genres"})
    except MusicBrainzError as exc:
        log.debug("musicbrainz genres failed for %s: %s", artist_mbid, exc)
        return []
    if not isinstance(data, dict):
        # Not an answer about this artist; a scan is worth more than its tags.
        return []

    voted = []
    for genre in data.get("genres") or []:
        if not isinstance(genre, dict):
            continue
        name = str(genre.get("name") or "").strip()
        # A genre that has been voted down to nothing is still listed, at zero
        # or below, and is the opposite of a recommendation.
        votes = genre.get("count")
        votes = 1 if votes is None else _votes(votes)
        if name and votes > 0:
            voted.append((votes, name))

    # Stable, so genres with equal votes keep MusicBrainz's own order.
    voted.sort(key=lambda pair: -pair[0])
    genres = [name for _, name in voted][:GENRES_PER_ARTIST]

    if len(_genre_cache) >= GENRE_CACHE_LIMIT:
        _genre_cache.clear()
    _genre_cache[artist_mbid] = genres
    return list(genres)


def _votes(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
