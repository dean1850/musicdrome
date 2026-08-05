"""Background scheduler.

APScheduler runs in-process rather than as a separate worker: SQLite is
single-file, the library is local, and a second container would only add
deployment surface. Every job is wrapped so one failing task can never take the
scheduler down with it, and `max_instances=1` keeps a slow scan from stacking up
behind itself.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from ..config import settings

log = logging.getLogger(__name__)

scheduler = BackgroundScheduler(
    timezone=settings.timezone,
    job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
)


def _guard(name: str, func: Callable) -> Callable:
    """Wrap a job so an exception is logged rather than killing the schedule."""

    def runner() -> None:
        try:
            func()
        except Exception:
            log.exception("scheduled job '%s' failed", name)

    runner.__name__ = f"job_{name}"
    return runner


def _every(name: str, func: Callable, *, minutes: float = 0, hours: float = 0,
           first_run_seconds: int | None = None) -> None:
    if minutes <= 0 and hours <= 0:
        log.info("job '%s' disabled (interval is zero)", name)
        return

    trigger = IntervalTrigger(minutes=minutes, hours=hours)
    scheduler.add_job(
        _guard(name, func),
        trigger=trigger,
        id=name,
        replace_existing=True,
        next_run_time=None if first_run_seconds is None else _in(first_run_seconds),
    )
    unit = f"{hours}h" if hours else f"{minutes}m"
    log.info("scheduled '%s' every %s", name, unit)


def _in(seconds: int):
    from datetime import datetime, timedelta

    return datetime.now() + timedelta(seconds=seconds)


def register_jobs() -> None:
    """Wire up every recurring task."""
    from . import acquisition, enrich, lidarr, podcasts, recommendations, scanner
    from . import scrobble, smartplaylist, transcode
    from .ai import analytics as ai_analytics
    from .ai import curator as ai_curator

    # ─── Library ───────────────────────────────────────────────────────
    _every(
        "library_scan",
        lambda: scanner.scan_library(full=False),
        minutes=settings.scan_interval_minutes,
    )

    # ─── Scrobbling — frequent and cheap ───────────────────────────────
    _every("scrobble_queue", scrobble.process_queue, minutes=1)
    _every("scrobble_prune", lambda: scrobble.prune_queue(30), hours=24)

    # ─── Playlists ─────────────────────────────────────────────────────
    if settings.smart_playlist_enabled:
        _every(
            "smart_playlists",
            smartplaylist.refresh_all,
            minutes=settings.smart_playlist_refresh_minutes,
            first_run_seconds=120,
        )

    # ─── AI ────────────────────────────────────────────────────────────
    if settings.ai_enabled:
        _every(
            "ai_playlists",
            ai_curator.refresh_ai_playlists,
            hours=settings.ai_playlist_refresh_hours,
            first_run_seconds=600,
        )
        _every(
            "ai_analytics",
            ai_analytics.refresh_reports,
            hours=settings.ai_analytics_refresh_hours,
            first_run_seconds=900,
        )

    # ─── Metadata enrichment ───────────────────────────────────────────
    if settings.lastfm_enabled or settings.musicbrainz_enabled:
        _every(
            "enrich_library",
            lambda: enrich.enrich_library(limit=50),
            hours=6,
            first_run_seconds=300,
        )
        _every(
            "enrich_track_mbids",
            lambda: enrich.enrich_track_mbids(limit=100),
            hours=12,
            first_run_seconds=1800,
        )

    # ─── Discovery and acquisition ─────────────────────────────────────
    if settings.recommendations_enabled:
        _every(
            "recommendations",
            recommendations.refresh_all,
            hours=settings.recommendation_refresh_hours,
            first_run_seconds=1200,
        )
        _every("recommendations_prune", lambda: recommendations.prune(60), hours=24)

    if settings.acquisition_enabled:
        _every(
            "acquisition_queue",
            lambda: acquisition.process_queue(limit=5),
            minutes=15,
            first_run_seconds=300,
        )

    if settings.lidarr_enabled:
        _every(
            "lidarr_sync",
            lidarr.sync,
            minutes=settings.lidarr_sync_interval_minutes,
            first_run_seconds=180,
        )

    # ─── Podcasts ──────────────────────────────────────────────────────
    if settings.podcast_enabled:
        _every(
            "podcast_refresh",
            podcasts.refresh_all,
            hours=settings.podcast_refresh_hours,
            first_run_seconds=240,
        )

    # ─── Housekeeping ──────────────────────────────────────────────────
    if settings.transcode_cache_enabled:
        _every("transcode_cache_prune", transcode.prune_cache, hours=1)


def start() -> None:
    if scheduler.running:
        return
    register_jobs()
    scheduler.start()
    log.info("scheduler started with %d jobs", len(scheduler.get_jobs()))


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        log.info("scheduler stopped")


def job_status() -> list[dict]:
    """Job list for the admin screen."""
    return [
        {
            "id": job.id,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            "trigger": str(job.trigger),
        }
        for job in scheduler.get_jobs()
    ]


def run_now(job_id: str) -> bool:
    """Trigger a scheduled job immediately."""
    job = scheduler.get_job(job_id)
    if job is None:
        return False
    job.modify(next_run_time=_in(1))
    return True
