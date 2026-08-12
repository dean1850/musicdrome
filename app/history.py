"""Listening history: pulling scrobbles in, and turning them into a taste profile.

Plays arrive from Last.fm and ListenBrainz and land in one table, tagged with
whose they are. Each (user, source) pair keeps its own cursor — the timestamp of
the newest play successfully stored — so a sync only ever asks for what it has
not seen, one person's unreachable profile cannot stall anybody else's, and
adding a second source later back-fills without disturbing the first.

The cursor advances only after the rows are committed. A sync that dies halfway
re-reads a page next time and the ``UNIQUE (user_id, track_key, played_at,
source)`` constraint absorbs the duplicates.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterator

from . import db, users
from .norm import artist_key, track_key
from .sources import lastfm, listenbrainz

log = logging.getLogger(__name__)

SOURCES: dict[str, tuple[Callable[..., bool], Callable[..., Iterator[dict]]]] = {
    "lastfm": (lastfm.configured, lastfm.recent_tracks),
    "listenbrainz": (listenbrainz.configured, listenbrainz.recent_tracks),
}


def _identity(user: dict[str, Any], source: str) -> dict[str, str]:
    """The arguments identifying one user to one history source."""
    credentials = users.credentials(user["id"])
    if source == "lastfm":
        return {"user": credentials.get("lastfm_user", "")}
    return {
        "user": credentials.get("listenbrainz_user", ""),
        "token": credentials.get("listenbrainz_token", ""),
    }


def _cursor(conn, user_id: int, source: str) -> int:
    row = conn.execute(
        "SELECT cursor FROM user_sync_state WHERE user_id = ? AND source = ?", (user_id, source)
    ).fetchone()
    return int(row["cursor"]) if row else 0


def _save_cursor(conn, user_id: int, source: str, cursor: int, error: str = "") -> None:
    conn.execute(
        "INSERT INTO user_sync_state (user_id, source, cursor, synced_at, error) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (user_id, source) DO UPDATE SET cursor = excluded.cursor, "
        "synced_at = excluded.synced_at, error = excluded.error",
        (user_id, source, cursor, db.now(), error[:300]),
    )


def sync(user_id: int | None = None) -> dict[str, Any]:
    """Pull new plays for one user, or for everyone if ``user_id`` is None."""
    people = [users.get(user_id)] if user_id else users.active_users()
    result: dict[str, Any] = {"added": 0, "sources": {}, "users": {}}

    for user in people:
        if not user:
            continue
        one = _sync_user(user)
        result["added"] += one["added"]
        result["users"][user["name"]] = one["sources"]
        # Totals per source across the household, so the existing status line
        # still has something to show without knowing about users.
        for name, detail in one["sources"].items():
            merged = result["sources"].setdefault(name, {"added": 0, "error": ""})
            merged["added"] += detail["added"]
            merged["error"] = merged["error"] or detail["error"]

    return result


def _sync_user(user: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"added": 0, "sources": {}}

    for name, (is_configured, fetch) in SOURCES.items():
        identity = _identity(user, name)
        if not is_configured(identity.get("user", "")):
            continue

        with db.connect() as conn:
            since = _cursor(conn, user["id"], name)

        added, newest, error = 0, since, ""
        try:
            batch: list[tuple] = []
            for play in fetch(since=since, **identity):
                batch.append(
                    (
                        user["id"],
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
            log.warning("%s sync failed for %s: %s", name, user["name"], exc)

        with db.connect() as conn:
            _save_cursor(conn, user["id"], name, newest, error)

        result["added"] += added
        result["sources"][name] = {"added": added, "cursor": newest, "error": error}
        log.info("%s/%s: %d new plays", user["name"], name, added)

    return result


def _insert(rows: list[tuple]) -> int:
    with db.connect() as conn:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO plays "
            "(user_id, artist, title, album, artist_key, track_key, played_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        return conn.total_changes - before


# ─── Taste profile ─────────────────────────────────────────────────────────


def profile(days: int = 90, user_id: int | None = None) -> dict[str, Any]:
    """What the AI is told about you.

    Three views of the same window, because they answer different questions:
    the most-played artists say what you like, the most-played tracks say what
    you like *by* them, and the recently discovered artists say where your taste
    is currently moving — which is usually the most useful signal of the three.

    Scoped to one user when given one. Without a ``user_id`` this reads the
    whole household's plays, which is what the stats page wants when nobody in
    particular is selected, and never what a scan wants.
    """
    since = db.now() - days * 86400
    # Applied to every query below, so a user's profile can never be widened by
    # somebody else's listening.
    scope = "AND user_id = ?" if user_id else ""
    params: list[Any] = [since, user_id] if user_id else [since]

    with db.connect() as conn:
        top_artists = [
            dict(row)
            for row in conn.execute(
                f"SELECT artist, COUNT(*) AS plays FROM plays WHERE played_at >= ? {scope} "
                "GROUP BY artist_key ORDER BY plays DESC, artist ASC LIMIT 40",
                params,
            )
        ]
        top_tracks = [
            dict(row)
            for row in conn.execute(
                f"SELECT artist, title, COUNT(*) AS plays FROM plays WHERE played_at >= ? {scope} "
                "GROUP BY track_key ORDER BY plays DESC, artist ASC LIMIT 40",
                params,
            )
        ]
        # Artists whose first-ever play falls inside the window. The user
        # filter belongs in WHERE, before the grouping, so "first ever" means
        # first for this listener rather than first for the household.
        discovery_scope = "WHERE user_id = ?" if user_id else ""
        recent_discoveries = [
            dict(row)
            for row in conn.execute(
                "SELECT artist, MIN(played_at) AS first_play, COUNT(*) AS plays FROM plays "
                f"{discovery_scope} GROUP BY artist_key HAVING first_play >= ? "
                "ORDER BY first_play DESC LIMIT 20",
                ([user_id, since] if user_id else [since]),
            )
        ]
        total = conn.execute(
            "SELECT COUNT(*) AS plays, COUNT(DISTINCT artist_key) AS artists FROM plays "
            f"WHERE played_at >= ? {scope}",
            params,
        ).fetchone()

    return {
        "days": days,
        "user_id": user_id,
        "plays": total["plays"] if total else 0,
        "artists": total["artists"] if total else 0,
        "top_artists": top_artists,
        "top_tracks": top_tracks,
        "recent_discoveries": recent_discoveries,
    }


def status(user_id: int | None = None) -> dict[str, Any]:
    """Per-source sync state, for the UI to show what is actually wired up.

    With a ``user_id`` this describes that person's sources. Without one it
    describes the household: a source counts as configured if anybody has it
    set up, and the error shown is the first one anybody hit.
    """
    with db.connect() as conn:
        if user_id:
            rows = {
                row["source"]: dict(row)
                for row in conn.execute(
                    "SELECT * FROM user_sync_state WHERE user_id = ?", (user_id,)
                )
            }
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM plays WHERE user_id = ?", (user_id,)
            ).fetchone()["n"]
        else:
            # Newest sync and any outstanding error, collapsed across users.
            rows = {
                row["source"]: dict(row)
                for row in conn.execute(
                    "SELECT source, MAX(synced_at) AS synced_at, "
                    "MAX(error) AS error FROM user_sync_state GROUP BY source"
                )
            }
            total = conn.execute("SELECT COUNT(*) AS n FROM plays").fetchone()["n"]

    people = [users.get(user_id)] if user_id else users.active_users()
    return {
        "total_plays": total,
        "sources": [
            {
                "name": name,
                "configured": any(
                    SOURCES[name][0](_identity(person, name).get("user", ""))
                    for person in people
                    if person
                ),
                "synced_at": (rows.get(name) or {}).get("synced_at"),
                "error": (rows.get(name) or {}).get("error", ""),
            }
            for name in SOURCES
        ],
    }
