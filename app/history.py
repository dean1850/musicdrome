"""Listening history: pulling scrobbles in, and turning them into a taste profile.

Plays arrive from Last.fm and ListenBrainz and land in one table. Each source
keeps its own cursor — the timestamp of the newest play successfully stored —
so a sync only ever asks for what it has not seen, and adding a second source
later back-fills without disturbing the first.

The cursor advances only after the rows are committed. A sync that dies halfway
re-reads a page next time and the ``UNIQUE (track_key, played_at, source)``
constraint absorbs the duplicates.

Navidrome is here too, and is a different shape on purpose. A scrobble is an
event; what Navidrome reports is a *state* — this track is hearted, this track
has been played 34 times, most recently on Tuesday. There is no way back from
that to the 34 listens, so :func:`sync_navidrome` keeps it as the aggregate it
is in its own table rather than manufacturing play rows to fit. Charting
invented timestamps on the stats page would be worse than not having the data.

Who you are comes from the environment: ``LASTFM_USER``, ``LISTENBRAINZ_USER``
and ``NAVIDROME_USER``. There is one listener.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterator

from . import config, db
from .norm import artist_key, track_key
from .sources import lastfm, listenbrainz, navidrome

log = logging.getLogger(__name__)

SOURCES: dict[str, tuple[Callable[..., bool], Callable[..., Iterator[dict]]]] = {
    "lastfm": (lastfm.configured, lastfm.recent_tracks),
    "listenbrainz": (listenbrainz.configured, listenbrainz.recent_tracks),
}


def _identity(source: str) -> dict[str, str]:
    """The arguments identifying the listener to one history source."""
    if source == "lastfm":
        return {"user": config.LASTFM_USER}
    return {"user": config.LISTENBRAINZ_USER, "token": config.LISTENBRAINZ_TOKEN}


def configured() -> bool:
    """Whether at least one scrobble account is usable."""
    return any(
        is_configured(_identity(name).get("user", ""))
        for name, (is_configured, _) in SOURCES.items()
    )


def _cursor(conn, source: str) -> int:
    row = conn.execute("SELECT cursor FROM sync_state WHERE source = ?", (source,)).fetchone()
    return int(row["cursor"]) if row else 0


def _save_cursor(conn, source: str, cursor: int, error: str = "") -> None:
    conn.execute(
        "INSERT INTO sync_state (source, cursor, synced_at, error) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (source) DO UPDATE SET cursor = excluded.cursor, "
        "synced_at = excluded.synced_at, error = excluded.error",
        (source, cursor, db.now(), error[:300]),
    )


def sync() -> dict[str, Any]:
    """Pull everything new from every configured source."""
    result: dict[str, Any] = {"added": 0, "sources": {}}

    for name, (is_configured, fetch) in SOURCES.items():
        identity = _identity(name)
        if not is_configured(identity.get("user", "")):
            continue

        with db.connect() as conn:
            since = _cursor(conn, name)

        added, newest, error = 0, since, ""
        try:
            batch: list[tuple] = []
            for play in fetch(since=since, **identity):
                batch.append(
                    (
                        play["artist"],
                        play["title"],
                        play.get("album", ""),
                        artist_key(play["artist"]),
                        track_key(play["artist"], play["title"]),
                        play["played_at"],
                        play["source"],
                    )
                )
                newest = max(newest, play["played_at"])
                if len(batch) >= 500:
                    added += _insert(batch)
                    batch.clear()
            if batch:
                added += _insert(batch)
        except Exception as exc:
            error = str(exc)
            log.warning("%s sync failed: %s", name, exc)

        with db.connect() as conn:
            _save_cursor(conn, name, newest, error)

        result["added"] += added
        result["sources"][name] = {"added": added, "cursor": newest, "error": error}
        log.info("%s: %d new plays", name, added)

    return result


def _insert(rows: list[tuple]) -> int:
    with db.connect() as conn:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO plays "
            "(artist, title, album, artist_key, track_key, played_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        return conn.total_changes - before


# ─── Navidrome ─────────────────────────────────────────────────────────────

# The sync_state rows this uses. Two, because the two halves run on different
# clocks: hearts are one request and refresh every scan, the library walk is
# hundreds and refreshes when it goes stale.
NAVIDROME_SOURCE = "navidrome"
NAVIDROME_LIBRARY_SOURCE = "navidrome-library"


def sync_navidrome(force: bool = False) -> dict[str, Any]:
    """Pull hearts, and the library play counts if they have gone stale.

    Order matters. The library walk runs first and reports whatever ``starred``
    state each song had at the moment it was read; ``getStarred2`` then runs
    over the top and is treated as the authority, because a walk of twenty
    thousand tracks takes long enough for a heart to be added or removed while
    it is in progress.

    Nothing here raises. A Navidrome that is down, moved or misconfigured must
    cost you the second signal and not the scan — the recommender worked
    without it before and still does. The reason is written to ``sync_state``
    so the Settings page can say what went wrong instead of quietly showing one
    fewer connection.
    """
    result: dict[str, Any] = {
        "configured": navidrome.configured(),
        "hearts": 0, "library": 0, "walked": False, "error": "",
    }
    if not result["configured"]:
        return result

    try:
        result["walked"] = _walk_navidrome_library(force=force)
        result["library"] = _navidrome_count()
        result["hearts"] = _sync_navidrome_hearts()
    except Exception as exc:
        result["error"] = str(exc)
        log.warning("navidrome sync failed: %s", exc)

    with db.connect() as conn:
        _save_cursor(conn, NAVIDROME_SOURCE, db.now(), result["error"])

    log.info(
        "navidrome: %d hearted, %d tracks known%s",
        result["hearts"], result["library"], "" if result["walked"] else " (library walk not due)",
    )
    return result


def _sync_navidrome_hearts() -> int:
    """Store the hearted tracks, and un-heart everything that is no longer.

    The un-hearting is the half that is easy to leave out and wrong to. Without
    it a track you deliberately un-starred keeps boosting recommendations
    forever, and — because hearted tracks are excluded from suggestions —
    stays banned from ever being suggested again, with nothing in the UI
    explaining why.
    """
    songs = navidrome.starred_songs()
    _upsert_navidrome(songs)

    ids = [song["id"] for song in songs if song["id"]]
    with db.connect() as conn:
        if ids:
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"UPDATE navidrome_tracks SET starred = 0, starred_at = 0 "
                f"WHERE starred = 1 AND id NOT IN ({placeholders})",
                ids,
            )
        else:
            conn.execute("UPDATE navidrome_tracks SET starred = 0, starred_at = 0")
    return len(songs)


def _walk_navidrome_library(force: bool = False) -> bool:
    """Refresh play counts from a full library walk. ``False`` if not due.

    The cursor is written only on a walk that finished, so an interrupted one
    is retried on the next scan rather than being remembered as fresh for the
    next six hours.
    """
    if config.NAVIDROME_LIBRARY_PAGE <= 0:
        return False

    with db.connect() as conn:
        last = _cursor(conn, NAVIDROME_LIBRARY_SOURCE)
    if not force and last and db.now() - last < config.NAVIDROME_LIBRARY_MAX_AGE:
        return False

    batch: list[dict[str, Any]] = []
    seen = 0
    for song in navidrome.library_songs():
        batch.append(song)
        seen += 1
        if len(batch) >= 500:
            _upsert_navidrome(batch)
            batch.clear()
    if batch:
        _upsert_navidrome(batch)

    with db.connect() as conn:
        _save_cursor(conn, NAVIDROME_LIBRARY_SOURCE, db.now())
    log.info("navidrome library walk: %d tracks read", seen)
    return True


def _upsert_navidrome(songs: list[dict[str, Any]]) -> None:
    """Store songs by Navidrome's own id, refreshing what can change.

    ``id`` is the key rather than ``track_key`` because Navidrome's id is the
    thing ``getStarred2`` and the walk agree on, and because two files that
    normalise to the same track key — a single and its album version — are two
    rows in Navidrome with two independent play counts.

    Play counts only ever climb, so they are merged with ``MAX`` rather than
    overwritten. That is not defensiveness for its own sake: every count in a
    Subsonic response is tagged ``omitempty``, so a track that has never been
    played and a track whose count the server did not send arrive as the same
    absent field. Overwriting on that would let the hearts call — which runs
    after the walk, and covers the tracks with the *most* history behind them —
    quietly zero the very play counts the walk had just spent hundreds of
    requests collecting.
    """
    if not songs:
        return
    now = db.now()
    rows = [
        (
            song["id"], song["artist"], song["title"], song["album"],
            artist_key(song["artist"]), track_key(song["artist"], song["title"]),
            song["genre"], song["year"], int(bool(song["starred"])), song["starred_at"],
            song["rating"], song["play_count"], song["played_at"], now,
        )
        for song in songs
        if song.get("id")
    ]
    with db.connect() as conn:
        conn.executemany(
            "INSERT INTO navidrome_tracks (id, artist, title, album, artist_key, track_key, "
            "genre, year, starred, starred_at, rating, play_count, played_at, synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET "
            "  artist = excluded.artist, title = excluded.title, album = excluded.album, "
            "  artist_key = excluded.artist_key, track_key = excluded.track_key, "
            "  genre = excluded.genre, year = excluded.year, "
            "  starred = excluded.starred, starred_at = excluded.starred_at, "
            "  rating = excluded.rating, "
            "  play_count = MAX(excluded.play_count, navidrome_tracks.play_count), "
            "  played_at = MAX(excluded.played_at, navidrome_tracks.played_at), "
            "  synced_at = excluded.synced_at",
            rows,
        )


def _navidrome_count() -> int:
    with db.connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM navidrome_tracks").fetchone()["n"]


# ─── Taste profile ─────────────────────────────────────────────────────────


def profile(days: int = 90) -> dict[str, Any]:
    """What the AI is told about you.

    Three views of the same window, because they answer different questions:
    the most-played artists say what you like, the most-played tracks say what
    you like *by* them, and the recently discovered artists say where your taste
    is currently moving — which is usually the most useful signal of the three.
    """
    since = db.now() - days * 86400

    with db.connect() as conn:
        top_artists = [
            dict(row)
            for row in conn.execute(
                "SELECT artist, COUNT(*) AS plays FROM plays WHERE played_at >= ? "
                "GROUP BY artist_key ORDER BY plays DESC, artist ASC LIMIT 40",
                (since,),
            )
        ]
        top_tracks = [
            dict(row)
            for row in conn.execute(
                "SELECT artist, title, COUNT(*) AS plays FROM plays WHERE played_at >= ? "
                "GROUP BY track_key ORDER BY plays DESC, artist ASC LIMIT 40",
                (since,),
            )
        ]
        # Artists whose first-ever play falls inside the window.
        recent_discoveries = [
            dict(row)
            for row in conn.execute(
                "SELECT artist, MIN(played_at) AS first_play, COUNT(*) AS plays FROM plays "
                "GROUP BY artist_key HAVING first_play >= ? ORDER BY first_play DESC LIMIT 20",
                (since,),
            )
        ]
        total = conn.execute(
            "SELECT COUNT(*) AS plays, COUNT(DISTINCT artist_key) AS artists FROM plays "
            "WHERE played_at >= ?",
            (since,),
        ).fetchone()

    return {
        "days": days,
        "plays": total["plays"] if total else 0,
        "artists": total["artists"] if total else 0,
        "top_artists": top_artists,
        "top_tracks": top_tracks,
        "recent_discoveries": recent_discoveries,
        **navidrome_profile(),
    }


def navidrome_profile() -> dict[str, Any]:
    """What you hearted and what you actually play, for the prompt.

    Unwindowed, unlike everything above it, and the difference is the point. A
    scrobble is only evidence of the moment it happened, so reading it through
    a ninety-day window is what keeps the profile current. A heart is a
    standing statement — you did not un-heart it when the window closed — and
    truncating those to the last ninety days would throw away almost all of
    them for the sake of a consistency that means nothing here.

    Empty dicts of the same shape when Navidrome is not set up, so the prompt
    builder can ask for these unconditionally.
    """
    empty = {"loved_tracks": [], "loved_artists": [], "loved_genres": [], "library_top_tracks": []}
    if not navidrome.configured():
        return empty

    with db.connect() as conn:
        loved_tracks = [
            dict(row)
            for row in conn.execute(
                "SELECT artist, title, starred_at FROM navidrome_tracks WHERE starred = 1 "
                # Newest first: what you hearted last month says more about
                # where your taste is now than what you hearted in 2019.
                "ORDER BY starred_at DESC, artist ASC LIMIT 120"
            )
        ]
        loved_artists = [
            dict(row)
            for row in conn.execute(
                "SELECT artist, COUNT(*) AS hearts FROM navidrome_tracks WHERE starred = 1 "
                "GROUP BY artist_key ORDER BY hearts DESC, artist ASC LIMIT 40"
            )
        ]
        loved_genres = [
            dict(row)
            for row in conn.execute(
                "SELECT genre, COUNT(*) AS hearts FROM navidrome_tracks "
                "WHERE starred = 1 AND genre != '' GROUP BY lower(genre) "
                "ORDER BY hearts DESC, genre ASC LIMIT 15"
            )
        ]
        library_top_tracks = [
            dict(row)
            for row in conn.execute(
                "SELECT artist, title, play_count FROM navidrome_tracks WHERE play_count > 0 "
                "ORDER BY play_count DESC, artist ASC LIMIT 30"
            )
        ]

    return {
        "loved_tracks": loved_tracks,
        "loved_artists": loved_artists,
        "loved_genres": loved_genres,
        "library_top_tracks": library_top_tracks,
    }


def status() -> dict[str, Any]:
    """Per-source sync state, for the UI to show what is actually wired up."""
    with db.connect() as conn:
        rows = {row["source"]: dict(row) for row in conn.execute("SELECT * FROM sync_state")}
        total = conn.execute("SELECT COUNT(*) AS n FROM plays").fetchone()["n"]
        hearts = conn.execute(
            "SELECT COUNT(*) AS n FROM navidrome_tracks WHERE starred = 1"
        ).fetchone()["n"]
        known = conn.execute("SELECT COUNT(*) AS n FROM navidrome_tracks").fetchone()["n"]

    navidrome_row = rows.get(NAVIDROME_SOURCE) or {}
    return {
        "total_plays": total,
        "sources": [
            {
                "name": name,
                "configured": SOURCES[name][0](_identity(name).get("user", "")),
                "synced_at": (rows.get(name) or {}).get("synced_at"),
                "error": (rows.get(name) or {}).get("error", ""),
            }
            for name in SOURCES
        ],
        # Kept out of `sources` rather than added to it: everything in that list
        # is polled by `sync()` and feeds the plays table, and Navidrome does
        # neither. The UI shows it as its own row for the same reason.
        "navidrome": {
            "configured": navidrome.configured(),
            "url": config.NAVIDROME_URL,
            "user": config.NAVIDROME_USER,
            "hearts": hearts,
            "tracks": known,
            "synced_at": navidrome_row.get("synced_at"),
            "error": navidrome_row.get("error", ""),
        },
    }
