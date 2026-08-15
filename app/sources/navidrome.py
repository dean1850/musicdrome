"""Navidrome — hearted tracks and library play counts.

The second input to the recommender. Everything else Musicdrome reads is a
*play*: something happened, a scrobble was recorded, and volume stands in for
preference. That conflates two very different things — a record you love and a
record that happened to be next in the queue for six hours.

Navidrome knows the difference, because you told it. A starred track is a
deliberate act, and there are two orders of magnitude fewer of them than there
are scrobbles. That scarcity is exactly what makes the signal worth having.

**On credentials.** Navidrome has no API key, and no amount of looking will find
one — its Subsonic API authenticates a *user*, via one of four shapes accepted
in ``server/subsonic/middlewares.go``: a plaintext password, a hex-encoded
password, an MD5 of the password salted per request, or a session JWT. This
module uses the salted MD5, which is what every Subsonic client uses and the
only one of the four that never puts the password itself in a URL — so it stays
out of Navidrome's access log, out of any reverse proxy in front of it, and out
of Musicdrome's own logs when a request is traced.

The salt is regenerated for every request rather than once per session. A fixed
salt would make the token a password equivalent for anyone who saw one request.

**On what is read.** Three endpoints, all read-only:

``ping``
    Whether the credentials work, and what is on the other end. Used by the
    connection status and nothing else.
``getStarred2``
    Every hearted song, album and artist in one response. Songs are what we
    keep. This is cheap, so it runs on every scan.
``search3`` with an empty query
    Subsonic's documented "give me everything" form, paged. The only way to
    read play counts, because no Subsonic endpoint lists played songs. This is
    the expensive one — see :func:`sync` for when it is skipped.

Each song carries its per-user annotations (``starred``, ``userRating``,
``playCount``, ``played``), because Navidrome joins the annotation table into
the search path. One walk therefore collects both signals at once.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Iterator

import httpx

from .. import config, net

log = logging.getLogger(__name__)

TIMEOUT = 30.0

# The version Navidrome reports compatibility with. Sent as `v`, and Navidrome
# rejects a request that omits it.
API_VERSION = "1.16.1"
CLIENT_NAME = "musicdrome"

# Subsonic's "match everything" query. Two literal quote characters, which is
# the form the spec settled on and which Navidrome special-cases in
# `persistence/sql_search.go` into "return all in natural order".
MATCH_ALL = '""'


class NavidromeError(RuntimeError):
    pass


def configured() -> bool:
    return config.navidrome_configured()


def auth_params() -> dict[str, str]:
    """Credentials for one request, with a salt that is used once.

    ``t = md5(password + salt)`` is the Subsonic token scheme. The password
    never travels; the salt does, in the clear, which is the design — it is
    there to stop a captured token being replayed against a different one, not
    to be secret.
    """
    salt = secrets.token_hex(8)
    token = hashlib.md5(f"{config.NAVIDROME_PASSWORD}{salt}".encode()).hexdigest()
    return {
        "u": config.NAVIDROME_USER,
        "t": token,
        "s": salt,
        "v": API_VERSION,
        "c": CLIENT_NAME,
        "f": "json",
    }


def _get(endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """One Subsonic call, unwrapped down to the payload.

    Subsonic answers HTTP 200 for everything, including a refused login, and
    puts the real outcome in ``subsonic-response.status``. Reading only the
    status code here would treat a wrong password as a successful sync that
    happened to find no music.
    """
    if not configured():
        raise NavidromeError("NAVIDROME_URL, NAVIDROME_USER and NAVIDROME_PASSWORD are not all set")

    url = f"{config.NAVIDROME_URL.rstrip('/')}/rest/{endpoint}"
    payload = {**auth_params(), **(params or {})}

    def fetch() -> httpx.Response:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
            return client.get(url, params=payload)

    try:
        response = net.with_retry(fetch, what=f"navidrome {endpoint}")
    except httpx.HTTPError as exc:
        raise NavidromeError(f"network error: {exc}") from exc

    if response.status_code >= 400:
        raise NavidromeError(f"HTTP {response.status_code}: {response.text[:200]}")

    try:
        body = response.json()
    except ValueError as exc:
        # Almost always NAVIDROME_URL pointing at something that is not
        # Navidrome — a reverse proxy's error page, or the wrong port.
        raise NavidromeError(
            f"{url} did not return JSON — check NAVIDROME_URL points at Navidrome itself"
        ) from exc

    data = body.get("subsonic-response") or {}
    if data.get("status") == "failed":
        error = data.get("error") or {}
        code, message = error.get("code"), error.get("message", "unknown error")
        if code == 40:
            raise NavidromeError(
                "Navidrome rejected the credentials — check NAVIDROME_USER and "
                "NAVIDROME_PASSWORD (this is your Navidrome login, not an API key)"
            )
        raise NavidromeError(f"Navidrome error {code}: {message}")
    if not data:
        raise NavidromeError(f"{url} answered without a subsonic-response body")
    return data


def ping() -> dict[str, Any]:
    """Prove the credentials work, and say what answered."""
    data = _get("ping.view")
    return {
        "ok": True,
        "version": data.get("version", ""),
        "server": data.get("type", "") or "subsonic",
        "server_version": data.get("serverVersion", ""),
    }


# ─── Reading ───────────────────────────────────────────────────────────────


def _song(entry: dict[str, Any]) -> dict[str, Any] | None:
    """One Subsonic ``Child`` as the fields we store, or ``None`` if unusable.

    ``starred`` is an ISO timestamp when set and absent otherwise, so its
    presence *is* the flag — there is no boolean to read.
    """
    artist = str(entry.get("artist") or entry.get("displayArtist") or "").strip()
    title = str(entry.get("title") or entry.get("name") or "").strip()
    if not artist or not title:
        return None

    return {
        "id": str(entry.get("id") or ""),
        "artist": artist,
        "title": title,
        "album": str(entry.get("album") or "").strip(),
        "genre": str(entry.get("genre") or "").strip(),
        "year": _int(entry.get("year")),
        "starred": bool(entry.get("starred")),
        "starred_at": _timestamp(entry.get("starred")),
        "rating": max(0, min(5, _int(entry.get("userRating")))),
        "play_count": max(0, _int(entry.get("playCount"))),
        "played_at": _timestamp(entry.get("played")),
    }


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


"""Fractional seconds, however many digits Go decided to print."""
_FRACTION = re.compile(r"\.(\d+)")


def _timestamp(value: Any) -> int:
    """An RFC 3339 timestamp as unix seconds, or 0.

    Navidrome is Go, so it writes nanoseconds — ``2024-03-11T21:04:07.123456789Z``
    — and trims trailing zeroes, so the same field can arrive with one digit or
    nine. ``datetime.fromisoformat`` accepts exactly three or six before Python
    3.11 and a bare ``Z`` only from 3.11, so both are normalised here rather
    than left to depend on which interpreter the image happens to ship.

    Padding matters as much as truncating: ``.1`` is a hundred milliseconds,
    not one microsecond, and reading it as the latter would be silently wrong
    in the direction nobody checks.
    """
    if not value or not isinstance(value, str):
        return 0

    text = value.strip()
    if text[-1:] in {"Z", "z"}:
        text = f"{text[:-1]}+00:00"
    text = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1)

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        log.debug("could not read the Navidrome timestamp %r", value)
        return 0

    # An offset-less timestamp from a server API is UTC. Left naive, it would
    # be read as the container's local time, which moves the whole starred
    # history by however many hours TZ happens to be.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def starred_songs() -> list[dict[str, Any]]:
    """Every hearted song, in one request.

    Albums and artists come back in the same response and are dropped: a
    hearted album is a statement about a record, and turning it into a
    statement about each of its tracks would invent a preference nobody
    expressed.
    """
    data = _get("getStarred2.view")
    entries = (data.get("starred2") or {}).get("song") or []
    songs = [song for song in (_song(entry) for entry in entries) if song]
    for song in songs:
        # getStarred2 returns these *because* they are starred, and older
        # Navidrome builds have been known to omit the attribute in this one
        # response. The endpoint is the evidence; trust it over the field.
        song["starred"] = True
    return songs


def library_songs(
    page_size: int | None = None, max_tracks: int | None = None
) -> Iterator[dict[str, Any]]:
    """Walk the whole library, yielding every song with its annotations.

    Paged with ``songOffset``. ``artistCount`` and ``albumCount`` are pinned to
    zero because Navidrome runs the three searches in parallel and we would
    otherwise pay for two of them on every page and throw both away.

    A short page is the end of the library. So is a page of songs we have
    already seen, which is the shape a server that ignores ``songOffset``
    produces — without that check a paging bug becomes an infinite loop rather
    than an error.
    """
    # ``None`` means "whatever is configured"; zero means "do not walk at all",
    # which is what NAVIDROME_LIBRARY_PAGE=0 is for. Collapsing those two into
    # a falsy check would make the off switch silently turn the walk back on.
    page_size = config.NAVIDROME_LIBRARY_PAGE if page_size is None else page_size
    max_tracks = config.NAVIDROME_MAX_TRACKS if max_tracks is None else max_tracks
    if page_size <= 0:
        return

    offset = 0
    seen: set[str] = set()

    while offset < max_tracks:
        data = _get(
            "search3.view",
            {
                "query": MATCH_ALL,
                "songCount": page_size,
                "songOffset": offset,
                "artistCount": 0,
                "albumCount": 0,
            },
        )
        entries = (data.get("searchResult3") or {}).get("song") or []
        if not entries:
            return

        fresh = 0
        for entry in entries:
            song = _song(entry)
            if not song or not song["id"] or song["id"] in seen:
                continue
            seen.add(song["id"])
            fresh += 1
            yield song

        if fresh == 0:
            log.warning(
                "navidrome returned no new songs at offset %d — stopping the library walk", offset
            )
            return
        if len(entries) < page_size:
            return
        offset += page_size

    log.warning(
        "stopped reading the Navidrome library at %d tracks (NAVIDROME_MAX_TRACKS)", max_tracks
    )
