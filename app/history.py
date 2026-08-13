"""Listening history: pulling scrobbles in, and turning them into a taste profile.

Plays arrive from Last.fm and ListenBrainz and land in one table. Each source
keeps its own cursor — the timestamp of the newest play successfully stored —
so a sync only ever asks for what it has not seen, and adding a second source
later back-fills without disturbing the first.

The cursor advances only after the rows are committed. A sync that dies halfway
re-reads a page next time and the ``UNIQUE (track_key, played_at, source)``
constraint absorbs the duplicates.

Who you are comes from the environment: ``LASTFM_USER`` and
``LISTENBRAINZ_USER``. There is one listener.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterator

from . import config, db
from .norm import artist_key, track_key
from .sources import lastfm, listenbrainz

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
    }


def status() -> dict[str, Any]:
    """Per-source sync state, for the UI to show what is actually wired up."""
    with db.connect() as conn:
        rows = {row["source"]: dict(row) for row in conn.execute("SELECT * FROM sync_state")}
        total = conn.execute("SELECT COUNT(*) AS n FROM plays").fetchone()["n"]

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
    }
