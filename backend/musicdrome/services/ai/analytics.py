"""Listening analytics.

Two layers:

* :func:`compute_stats` — pure SQL aggregates over ``play_history``. Always
  available, no AI required, and what the dashboard charts are drawn from.
* :func:`generate_insights` — hands those aggregates to the model for a written
  read of the user's listening: what changed, what it suggests, what to try next.

Reports are cached in ``ai_reports`` and regenerated on a schedule so the
dashboard never blocks on a model call.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...config import settings
from ...db import session_scope, utcnow
from ...models import AIReport, PlayHistory, User
from .provider import AIError, get_provider

log = logging.getLogger(__name__)

PERIODS: dict[str, int | None] = {
    "week": 7,
    "month": 30,
    "quarter": 90,
    "year": 365,
    "all": None,
}

SYSTEM_PROMPT = """You are a music analyst writing a personal listening report.

You are given aggregate statistics from one user's listening history on their
own music server. Write for that user, in second person, with specifics — cite
their actual artists, genres, and numbers rather than generalities.

Be honest and observational rather than flattering. If the data shows a narrow
rotation, say so. If something looks like a phase or a shift, name it. Skip
anything the numbers don't support.
"""

INSIGHTS_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {
            "type": "string",
            "description": "One sentence capturing the period, e.g. 'A month of late-night jazz.'",
        },
        "summary": {
            "type": "string",
            "description": "Two or three paragraphs on how they listened this period.",
        },
        "observations": {
            "type": "array",
            "description": "Three to five specific, data-grounded observations.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["title", "detail"],
                "additionalProperties": False,
            },
        },
        "listening_personality": {
            "type": "string",
            "description": "A short label for their listening style, plus one line of justification.",
        },
        "suggestions": {
            "type": "array",
            "description": "Two to four concrete suggestions: artists to revisit, gaps to explore.",
            "items": {"type": "string"},
        },
    },
    "required": ["headline", "summary", "observations", "listening_personality", "suggestions"],
    "additionalProperties": False,
}


# ─── Aggregates ────────────────────────────────────────────────────────────


def _window(period: str) -> datetime | None:
    days = PERIODS.get(period, 30)
    return None if days is None else utcnow() - timedelta(days=days)


def compute_stats(db: Session, user: User, period: str = "month") -> dict:
    """Aggregate one user's listening history over a period."""
    since = _window(period)

    def scoped(stmt):
        stmt = stmt.where(PlayHistory.user_id == user.id)
        return stmt.where(PlayHistory.played_at >= since) if since else stmt

    total_plays = db.scalar(scoped(select(func.count(PlayHistory.id)))) or 0
    total_seconds = db.scalar(
        scoped(select(func.coalesce(func.sum(PlayHistory.duration), 0)))
    ) or 0
    unique_artists = db.scalar(
        scoped(select(func.count(func.distinct(PlayHistory.artist_name))))
    ) or 0
    unique_tracks = db.scalar(
        scoped(select(func.count(func.distinct(PlayHistory.title))))
    ) or 0
    unique_albums = db.scalar(
        scoped(select(func.count(func.distinct(PlayHistory.album_name))))
    ) or 0

    def top(column, limit: int = 10) -> list[dict]:
        rows = db.execute(
            scoped(
                select(column, func.count(PlayHistory.id).label("plays"))
            )
            .where(column != "")
            .group_by(column)
            .order_by(func.count(PlayHistory.id).desc())
            .limit(limit)
        ).all()
        return [{"name": name, "plays": plays} for name, plays in rows]

    # Listening clock — SQLite strftime keeps this a single query
    hour_rows = db.execute(
        scoped(
            select(
                func.strftime("%H", PlayHistory.played_at).label("hour"),
                func.count(PlayHistory.id),
            )
        ).group_by("hour")
    ).all()
    by_hour = {f"{h:02d}": 0 for h in range(24)}
    for hour, count in hour_rows:
        if hour is not None:
            by_hour[hour] = count

    weekday_rows = db.execute(
        scoped(
            select(
                func.strftime("%w", PlayHistory.played_at).label("dow"),
                func.count(PlayHistory.id),
            )
        ).group_by("dow")
    ).all()
    day_names = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    by_weekday = {name: 0 for name in day_names}
    for dow, count in weekday_rows:
        if dow is not None and dow.isdigit():
            by_weekday[day_names[int(dow)]] = count

    daily_rows = db.execute(
        scoped(
            select(
                func.date(PlayHistory.played_at).label("day"),
                func.count(PlayHistory.id),
            )
        ).group_by("day").order_by("day")
    ).all()
    daily = [{"date": day, "plays": count} for day, count in daily_rows if day]

    # Artists heard for the first time inside the window
    new_artists: list[str] = []
    if since:
        seen_before = {
            row[0]
            for row in db.execute(
                select(func.distinct(PlayHistory.artist_name)).where(
                    PlayHistory.user_id == user.id, PlayHistory.played_at < since
                )
            ).all()
        }
        in_window = {
            row[0]
            for row in db.execute(
                scoped(select(func.distinct(PlayHistory.artist_name)))
            ).all()
        }
        new_artists = sorted(in_window - seen_before)

    return {
        "period": period,
        "generated_at": utcnow().isoformat(),
        "totals": {
            "plays": total_plays,
            "listening_seconds": int(total_seconds),
            "listening_hours": round(total_seconds / 3600, 1),
            "unique_artists": unique_artists,
            "unique_albums": unique_albums,
            "unique_tracks": unique_tracks,
            "avg_plays_per_day": (
                round(total_plays / max(PERIODS.get(period) or 1, 1), 1)
                if PERIODS.get(period)
                else None
            ),
        },
        "top_artists": top(PlayHistory.artist_name),
        "top_albums": top(PlayHistory.album_name),
        "top_tracks": top(PlayHistory.title),
        "top_genres": top(PlayHistory.genre),
        "by_hour": by_hour,
        "by_weekday": by_weekday,
        "daily": daily,
        "new_artists": new_artists[:25],
        "new_artist_count": len(new_artists),
        "listening_streak_days": _streak(daily),
        "repeat_ratio": (
            round(1 - (unique_tracks / total_plays), 3) if total_plays else 0.0
        ),
    }


def _streak(daily: list[dict]) -> int:
    """Longest run of consecutive days with at least one play."""
    if not daily:
        return 0
    dates = sorted(
        datetime.strptime(entry["date"], "%Y-%m-%d").date()
        for entry in daily
        if entry.get("date")
    )
    longest = current = 1
    for previous, day in zip(dates, dates[1:]):
        if (day - previous).days == 1:
            current += 1
            longest = max(longest, current)
        else:
            current = 1
    return longest


def compare_periods(db: Session, user: User, period: str = "month") -> dict:
    """This period against the one before it."""
    days = PERIODS.get(period)
    if not days:
        return {}

    now = utcnow()
    current_start = now - timedelta(days=days)
    previous_start = now - timedelta(days=days * 2)

    def window_stats(start: datetime, end: datetime) -> dict:
        base = select(func.count(PlayHistory.id)).where(
            PlayHistory.user_id == user.id,
            PlayHistory.played_at >= start,
            PlayHistory.played_at < end,
        )
        plays = db.scalar(base) or 0
        artists = db.scalar(
            select(func.count(func.distinct(PlayHistory.artist_name))).where(
                PlayHistory.user_id == user.id,
                PlayHistory.played_at >= start,
                PlayHistory.played_at < end,
            )
        ) or 0
        return {"plays": plays, "artists": artists}

    current = window_stats(current_start, now)
    previous = window_stats(previous_start, current_start)

    def delta(a: int, b: int) -> float:
        if not b:
            return 100.0 if a else 0.0
        return round(((a - b) / b) * 100, 1)

    return {
        "current": current,
        "previous": previous,
        "play_change_pct": delta(current["plays"], previous["plays"]),
        "artist_change_pct": delta(current["artists"], previous["artists"]),
    }


def genre_mix(db: Session, user: User, period: str = "month") -> list[dict]:
    """Genre share, handling multi-genre strings like 'Rock; Indie'."""
    since = _window(period)
    stmt = select(PlayHistory.genre).where(
        PlayHistory.user_id == user.id, PlayHistory.genre != ""
    )
    if since:
        stmt = stmt.where(PlayHistory.played_at >= since)

    counter: Counter[str] = Counter()
    for (genre,) in db.execute(stmt).all():
        for part in str(genre).replace("/", ";").split(";"):
            name = part.strip()
            if name:
                counter[name] += 1

    total = sum(counter.values()) or 1
    return [
        {"genre": name, "plays": count, "share": round(count / total, 4)}
        for name, count in counter.most_common(20)
    ]


# ─── AI narrative ──────────────────────────────────────────────────────────


def generate_insights(db: Session, user: User, period: str = "month") -> dict:
    """Model-written report over the computed statistics."""
    stats = compute_stats(db, user, period)
    if stats["totals"]["plays"] < settings.ai_min_plays_for_profile:
        raise AIError(
            f"not enough listening history yet — {stats['totals']['plays']} plays, "
            f"need {settings.ai_min_plays_for_profile}"
        )

    provider = get_provider()
    payload = {
        **stats,
        "genre_mix": genre_mix(db, user, period),
        "comparison": compare_periods(db, user, period),
    }
    prompt = (
        f"Listening statistics for the last {period}:\n\n"
        f"{json.dumps(payload, indent=2, default=str)}"
    )

    result = provider.complete_json(SYSTEM_PROMPT, prompt, schema=INSIGHTS_SCHEMA)
    if not isinstance(result, dict):
        raise AIError("model did not return an insights object")

    result["model"] = provider.model
    result["stats"] = stats
    return result


def get_or_create_report(
    db: Session, user: User, period: str = "month", *, force: bool = False
) -> AIReport | None:
    """Return a cached report, regenerating it when stale."""
    cutoff = utcnow() - timedelta(hours=settings.ai_analytics_refresh_hours)

    existing = db.scalar(
        select(AIReport)
        .where(
            AIReport.user_id == user.id,
            AIReport.kind == "listening_report",
            AIReport.period == period,
        )
        .order_by(AIReport.created_at.desc())
        .limit(1)
    )
    if existing is not None and not force and existing.created_at >= cutoff:
        return existing

    try:
        insights = generate_insights(db, user, period)
    except AIError as exc:
        log.info("insights unavailable for %s (%s): %s", user.username, period, exc)
        return existing

    report = AIReport(
        user_id=user.id,
        kind="listening_report",
        period=period,
        payload=insights,
        summary=insights.get("headline", ""),
        model=insights.get("model", ""),
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return report


def refresh_reports() -> dict[str, int]:
    """Rebuild stale listening reports for every opted-in user."""
    stats = {"generated": 0, "skipped": 0}
    if not settings.ai_enabled:
        return stats

    with session_scope() as db:
        users = db.scalars(
            select(User).where(User.is_active.is_(True), User.ai_enabled.is_(True))
        ).all()
        for user in users:
            try:
                before = db.scalar(
                    select(func.count(AIReport.id)).where(AIReport.user_id == user.id)
                )
                get_or_create_report(db, user, "month")
                after = db.scalar(
                    select(func.count(AIReport.id)).where(AIReport.user_id == user.id)
                )
                if (after or 0) > (before or 0):
                    stats["generated"] += 1
                else:
                    stats["skipped"] += 1
            except Exception:
                log.exception("failed to refresh report for %s", user.username)
                db.rollback()

    return stats
