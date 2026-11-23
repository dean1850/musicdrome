"""Maloja — listening history from your own scrobble server.

`Maloja <https://github.com/krateng/maloja>`_ is a self-hosted scrobble
database: players and bridges such as multi-scrobbler submit to it the way they
would to Last.fm, and it keeps every play on a machine you own. Musicdrome
reads it exactly as it reads Last.fm and ListenBrainz — new plays go into the
same table, behind the same cursor — so a Maloja install can replace either of
them outright.

**There is no key to set.** Maloja's read API is public: the web interface
shows your scrobbles to anyone who can reach it, and the API behind it asks for
nothing more. Its API keys exist so a *player* can submit — Maloja's own code
says that "an api key now ONLY permits scrobbling tracks, no other admin
tasks" — and Musicdrome never submits anything. So the whole configuration is
where the server is: ``MALOJA_URL``. A key would be a setting that does
nothing, and a setting that does nothing is worse than none.

**What is read.** One endpoint, ``/apis/mlj_1/scrobbles``, which answers
newest-first and pages with ``page`` (from zero) and ``perpage``. Its time
range parameters take calendar dates — ``2024/03/11``, ``2024/W10``, ``today``
— and not timestamps, so there is no asking for "everything after this
second". The walk therefore starts at the newest play and stops at the cursor,
which is the same shape :mod:`app.sources.listenbrainz` uses.

It stops a little *past* the cursor on purpose. Maloja receives plays late as a
matter of course: a phone that was offline scrobbles from its cache when it
reconnects, and multi-scrobbler catches up after Maloja itself was down — both
with the original timestamps, which land behind plays already read. Re-reading
the last :data:`LOOKBACK` seconds before the cursor catches those, and costs
nothing but duplicates that the plays table's ``UNIQUE`` constraint absorbs.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterator

import httpx

from .. import config, net

log = logging.getLogger(__name__)

TIMEOUT = 30.0

# Larger pages than Last.fm's, because this is a server on your own network with
# no rate limit to be polite to. Maloja's paging is an offset, though — every
# page re-reads the ones before it — so a first sync is still capped rather than
# left to walk a decade of history in one scan. Ten thousand plays is several
# months even for a heavy listener, which is more than the listening window
# reaches by default.
PAGE_SIZE = 500
MAX_PAGES = 20

# How far behind the cursor each sync looks again for plays that arrived late.
LOOKBACK = 86400  # one day

# Where the native API lives under the server's address. People paste either
# form — the API explorer shows the long one — so both are accepted.
API_PATH = "/apis/mlj_1"


class MalojaError(RuntimeError):
    pass


def configured(user: str = "") -> bool:
    """Whether a Maloja server is set.

    There is no user to name, and ``user`` is accepted only so that every
    history source can be asked the same question. A Maloja server holds one
    person's scrobbles: the address is the identity.
    """
    return bool(base_url())


def base_url() -> str:
    """``MALOJA_URL`` as the server's root, however it was written.

    A trailing ``/apis/mlj_1`` is dropped, and a missing scheme is taken to be
    ``http://`` — ``maloja:42010`` is how a compose service name is usually
    written, and httpx would otherwise read ``maloja`` as the protocol.
    """
    url = (config.MALOJA_URL or "").strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = f"http://{url}"
    for suffix in (API_PATH, "/apis"):
        if url.lower().endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
    return url


# The `user:password@` part of a URL. httpx sends it as HTTP basic auth, which
# is what makes a Maloja behind a login proxy work with no setting of its own —
# and exactly why it must never be repeated in an error message, which ends up
# in the log and on the Settings page. Greedy, so it runs to the *last* `@`
# before the path: a password with an `@` in it is still taken out whole.
_USERINFO = re.compile(r"(?<=://)[^/?#\s]*@")


def _redact(text: str) -> str:
    """``text`` with any login taken out of the URLs in it."""
    return _USERINFO.sub("", text)


def _get(endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """One native API call, as the decoded JSON body.

    Maloja reports its own failures properly — a status code and an ``error``
    object with a description — so those are passed on in its words. The
    failures it cannot describe are the ones where something else answered:
    a reverse proxy's login page, or a URL pointing at the wrong service.
    """
    root = base_url()
    if not root:
        raise MalojaError("MALOJA_URL is not set")

    url = f"{root}{API_PATH}/{endpoint}"
    shown = _redact(url)

    def fetch() -> httpx.Response:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
            return client.get(url, params=params or {}, headers={"Accept": "application/json"})

    try:
        response = net.with_retry(fetch, what=f"maloja {endpoint}")
    except httpx.HTTPError as exc:
        raise MalojaError(_redact(f"network error reaching {root}: {exc}")) from exc

    try:
        body = response.json()
    except ValueError:
        body = None

    # `shown` is already redacted, and it is the only URL these messages carry.
    if response.status_code >= 400:
        raise MalojaError(_http_failure(response, body, shown))
    if not isinstance(body, dict):
        raise MalojaError(
            f"{shown} did not return JSON — check MALOJA_URL is the address of Maloja "
            f"itself, such as http://maloja:42010"
        )
    if body.get("status") in {"error", "failure"}:
        raise MalojaError(f"Maloja: {_description(body) or 'unknown error'}")
    return body


def _http_failure(response: httpx.Response, body: Any, url: str) -> str:
    """Why a request failed, in terms of what to change."""
    code = response.status_code
    described = _description(body)
    if described:
        return f"Maloja answered HTTP {code}: {described}"
    if code in {401, 403}:
        # Maloja's read API asks for nothing, so a refusal here is something in
        # front of it. Saying so saves a search for a key that does not exist.
        return (
            f"HTTP {code} from {url} — Maloja needs no key to read scrobbles, so "
            f"this is a proxy in front of it asking for a login. Let /apis/mlj_1/ "
            f"through it, or, if it takes basic auth, put the login in the address: "
            f"MALOJA_URL=https://user:password@host"
        )
    if code == 404:
        return (
            f"HTTP 404 from {url} — check MALOJA_URL is the address of Maloja "
            f"itself, such as http://maloja:42010"
        )
    return f"HTTP {code} from {url}: {response.text[:200]}"


def _description(body: Any) -> str:
    """The human half of a Maloja error body, if there is one."""
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("desc") or error.get("type") or "").strip()
    if isinstance(error, str):
        return error.strip()
    return ""


def server_info() -> dict[str, Any]:
    """What answered, for checking a configuration by hand."""
    data = _get("serverinfo")
    return {
        "name": str(data.get("name") or ""),
        "version": str(data.get("versionstring") or ""),
    }


# ─── Listening history ─────────────────────────────────────────────────────


def recent_tracks(since: int = 0, max_pages: int = MAX_PAGES) -> Iterator[dict[str, Any]]:
    """Yield plays newer than ``since`` (unix seconds), newest first.

    Plays from the :data:`LOOKBACK` window behind ``since`` are yielded again,
    for the reason in the module docstring. The cursor is unaffected — it is
    the newest play stored, and nothing in that window is newer.

    A page shorter than ``perpage`` is the end of the history. A full one is
    not proof of more: Maloja offers a next page whenever the last was full,
    including when the history ends exactly on its boundary, and that page
    then arrives empty — which also stops the walk.
    """
    floor = max(0, since - LOOKBACK) if since else 0

    for page in range(max_pages):
        data = _get("scrobbles", {"page": page, "perpage": PAGE_SIZE})
        entries = data.get("list")
        if not isinstance(entries, list) or not entries:
            return

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            played_at = _int(entry.get("time"))
            if not played_at:
                continue
            if played_at <= floor:
                return  # caught up: everything from here down is already stored
            play = _play(entry, played_at)
            if play:
                yield play

        if len(entries) < PAGE_SIZE:
            return

    log.info(
        "maloja: stopped after the newest %d scrobbles — older ones are not read",
        max_pages * PAGE_SIZE,
    )


def _play(entry: dict[str, Any], played_at: int) -> dict[str, Any] | None:
    """One scrobble as a play row, or ``None`` if it names no track.

    Two shapes, because two Maloja generations are still in use. 3.x nests the
    track and gives the album as an object with its own artists::

        {"time": ..., "track": {"artists": [...], "title": ...,
                                "album": {"albumtitle": ..., "artists": [...]}}}

    2.x is flat, with the album — when there is one — as a bare string::

        {"time": ..., "artists": [...], "title": ..., "album": "..."}
    """
    track = entry.get("track")
    if not isinstance(track, dict):
        track = entry

    artists = _names(track.get("artists"))
    title = _text(track.get("title"))
    if not artists or not title:
        return None

    album = track.get("album")
    if isinstance(album, dict):
        album_title = _text(album.get("albumtitle") or album.get("title"))
        album_artists = _names(album.get("artists"))
    else:
        album_title, album_artists = _text(album), []

    artist, title = _artist_and_title(artists, album_artists, title)
    return {
        "artist": artist,
        "title": title,
        "album": album_title,
        "played_at": played_at,
        "source": "maloja",
    }


# A title that already names its guests, in the spellings players use.
_FEATURING = re.compile(r"\b(?:feat|ft|featuring)\b", re.IGNORECASE)


def _artist_and_title(
    artists: list[str], album_artists: list[str], title: str
) -> tuple[str, str]:
    """One play's artist credit and title, from Maloja's list of artists.

    Maloja splits "Daft Punk feat. Pharrell Williams" into two artists and
    hands them back in database order, not credit order — so once Pharrell has
    been scrobbled before, the track arrives as ``["Pharrell Williams", "Daft
    Punk"]``. The order is not cosmetic: :func:`app.norm.artist_key` keys a
    credit on its first name, and that key groups your plays by artist and
    decides what you already have.

    **When the album says whose record it is,** that settles it. *Random Access
    Memories* is Daft Punk's, so the album artists name the lead and anyone
    else on the track is a guest on it, which is written the way every player
    and tagger writes it::

        Daft Punk — Get Lucky (feat. Pharrell Williams)

    Guests in the title rather than the artist keep the profile's artist list
    reading "Daft Punk" instead of a different pairing for every
    collaboration, and cost nothing in matching — ``title_key`` drops a
    "(feat. …)" suffix, so the track key is the one any other source gives.
    This only applies when a player actually named the album artist: given an
    album and no album artist, Maloja copies the track's artists across, and
    then there is no guest to tell apart.

    **Otherwise the names are joined back** — "A, B & C" — rather than cut down
    to the first, because Maloja's splitting is not only for features. Out of
    the box ``&`` is one of its delimiters, so *Simon & Garfunkel* arrives as
    two artists and *Earth, Wind & Fire* as "Earth, Wind" and "Fire". Keeping
    only the first name would put "Simon" in your profile; joining with " & "
    gives the band its name back, and reads as an ordinary collaboration when
    that is what it was. Maloja's order stands here, which is usually the
    credit order: a new featured artist is created after the artist they
    feature with, and so sorts after them. ``artist_key`` cuts at the first
    comma or ampersand, so the key is the first name's either way.
    """
    album_names = {name.casefold() for name in album_artists}
    leads = [name for name in artists if name.casefold() in album_names]
    guests = [name for name in artists if name.casefold() not in album_names]

    if leads and guests:
        if not _FEATURING.search(title):
            title = f"{title} (feat. {_join(guests)})"
        return _join(leads), title
    return _join(artists), title


def _join(names: list[str]) -> str:
    """``["A", "B", "C"]`` as "A, B & C"."""
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} & {names[-1]}"


def _names(value: Any) -> list[str]:
    """A list of artist names, deduplicated, in the order given."""
    if isinstance(value, (str, dict)):
        value = [value]
    if not isinstance(value, list):
        return []

    names: list[str] = []
    seen: set[str] = set()
    for item in value:
        name = _text(item.get("name") if isinstance(item, dict) else item)
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            names.append(name)
    return names


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
