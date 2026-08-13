"""Listening statistics, computed straight from the plays table.

Everything here is SQL over scrobbles you already have, so the stats page costs
nothing to open and works with the AI backend switched off. The one exception is
:func:`taste_summary`, which spends a single AI call a day and caches the result.

Times are bucketed in the container's local timezone, set by ``TZ``. Plotting a
listening clock in UTC tells you when your server thinks you were awake.
"""

from __future__ import annotations

import logging
from typing import Any

from . import ai, db

log = logging.getLogger(__name__)

SUMMARY_CACHE_KEY = "taste_summary"
SUMMARY_MAX_AGE = 86400  # one call per day at most

SUMMARY_SYSTEM = """You describe one listener's music taste from their scrobbles.

Write two or three sentences in second person. Name the threads that actually
run through the data — genres, eras, moods, how broad or focused it is, what has
changed recently. Be specific and warm, never flattering, and never list the
artists back verbatim. No preamble, no headings.
"""


def overview(days: int = 90) -> dict[str, Any]:
    """Everything the stats page draws, in one query batch."""
    since = db.now() - days * 86400
    # Reused by every windowed query below.
    params: list[Any] = [since]

    with db.connect() as conn:
        totals = conn.execute(
            "SELECT COUNT(*) AS plays, COUNT(DISTINCT artist_key) AS artists, "
            "       COUNT(DISTINCT track_key) AS tracks "
            "FROM plays WHERE played_at >= ?",
            params,
        ).fetchone()

        top_artists = [
            dict(row)
            for row in conn.execute(
                "SELECT artist, COUNT(*) AS plays FROM plays WHERE played_at >= ? "
                "GROUP BY artist_key ORDER BY plays DESC, artist ASC LIMIT 15",
                params,
            )
        ]

        top_tracks = [
            dict(row)
            for row in conn.execute(
                "SELECT artist, title, COUNT(*) AS plays FROM plays WHERE played_at >= ? "
                "GROUP BY track_key ORDER BY plays DESC, artist ASC LIMIT 15",
                params,
            )
        ]

        daily = [
            dict(row)
            for row in conn.execute(
                "SELECT date(played_at, 'unixepoch', 'localtime') AS day, COUNT(*) AS plays "
                "FROM plays WHERE played_at >= ? GROUP BY day ORDER BY day",
                params,
            )
        ]

        clock_rows = {
            int(row["hour"]): row["plays"]
            for row in conn.execute(
                "SELECT CAST(strftime('%H', played_at, 'unixepoch', 'localtime') AS INTEGER) AS hour, "
                "       COUNT(*) AS plays "
                "FROM plays WHERE played_at >= ? GROUP BY hour",
                params,
            )
        }

        # A play is "new" when the track was first heard inside this window.
        freshness = conn.execute(
            "SELECT SUM(CASE WHEN first_play >= ? THEN plays ELSE 0 END) AS new_plays, "
            "       SUM(CASE WHEN first_play <  ? THEN plays ELSE 0 END) AS familiar_plays "
            "FROM (SELECT track_key, MIN(played_at) AS first_play, "
            "             SUM(CASE WHEN played_at >= ? THEN 1 ELSE 0 END) AS plays "
            "      FROM plays GROUP BY track_key)",
            (since, since, since),
        ).fetchone()

        sources = [
            dict(row)
            for row in conn.execute(
                "SELECT source, COUNT(*) AS plays FROM plays WHERE played_at >= ? "
                "GROUP BY source ORDER BY plays DESC",
                params,
            )
        ]

        library = conn.execute(
            "SELECT COUNT(*) AS downloaded, COALESCE(SUM(bytes), 0) AS bytes "
            "FROM downloads WHERE status = 'done'"
        ).fetchone()

    return {
        "days": days,
        "plays": totals["plays"],
        "artists": totals["artists"],
        "tracks": totals["tracks"],
        "top_artists": top_artists,
        "top_tracks": top_tracks,
        "daily": daily,
        "clock": [{"hour": hour, "plays": clock_rows.get(hour, 0)} for hour in range(24)],
        "new_plays": (freshness["new_plays"] or 0) if freshness else 0,
        "familiar_plays": (freshness["familiar_plays"] or 0) if freshness else 0,
        "sources": sources,
        "downloaded": library["downloaded"],
        "downloaded_bytes": library["bytes"],
    }


def taste_summary(days: int = 90, force: bool = False) -> dict[str, Any]:
    """A short AI-written description of your taste, refreshed daily.

    Returns a payload rather than a bare string so the page can distinguish "not
    generated yet" from "generated and empty", and can say why when the AI
    backend is unreachable.
    """
    if not db.get_setting("taste_summary"):
        return {"enabled": False, "text": "", "error": ""}

    if not force:
        cached = db.cache_get(SUMMARY_CACHE_KEY, SUMMARY_MAX_AGE)
        if cached:
            return {"enabled": True, "cached": True, **cached}

    if not ai.available():
        return {"enabled": True, "text": "", "error": f"{ai.provider()} is not configured"}

    from . import history

    profile = history.profile(days=days)
    if profile["plays"] < 20:
        return {"enabled": True, "text": "", "error": "not enough listening history yet"}

    prompt = "\n".join(
        [
            f"{profile['plays']} plays across {profile['artists']} artists in {days} days.",
            "",
            "Most played artists: "
            + ", ".join(f"{a['artist']} ({a['plays']})" for a in profile["top_artists"][:25]),
            "",
            "Most played tracks: "
            + ", ".join(f"{t['artist']} — {t['title']}" for t in profile["top_tracks"][:20]),
            "",
            "Recently discovered: "
            + (", ".join(d["artist"] for d in profile["recent_discoveries"][:12]) or "nothing new"),
        ]
    )

    try:
        text = ai.complete(SUMMARY_SYSTEM, prompt).strip()
    except Exception as exc:
        log.warning("taste summary failed: %s", exc)
        return {"enabled": True, "text": "", "error": str(exc)[:200]}

    payload = {"text": text, "error": "", "days": days}
    db.cache_put(SUMMARY_CACHE_KEY, payload)
    return {"enabled": True, "cached": False, **payload}
