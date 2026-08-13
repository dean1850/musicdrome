"""Application entry point.

Boots the database, starts the download workers, arms the scan schedule, and
serves the static UI. Everything long-running happens on background threads;
the request path only ever reads SQLite.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, config, db, download, scan
from .routes import router

log = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone=config.TIMEZONE)

SCHEDULE_HOURS = {"6h": 6, "daily": 24, "weekly": 168}


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    for noisy in ("httpx", "httpcore", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _scheduled_scan() -> None:
    log.info("scheduled scan starting")
    scan.run_in_background("scheduled")


def reschedule() -> None:
    """Apply the current schedule setting. Called at boot and on every save."""
    schedule = db.get_setting("schedule")
    hours = SCHEDULE_HOURS.get(schedule)

    if scheduler.get_job("scan"):
        scheduler.remove_job("scan")

    if hours is None:
        log.info("scheduled scans are off")
        return

    scheduler.add_job(
        _scheduled_scan,
        trigger=IntervalTrigger(hours=hours),
        id="scan",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    log.info("scans scheduled every %d hours", hours)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    log.info("Musicdrome %s starting", __version__)
    log.info("music:   %s", config.MUSIC_DIR)
    log.info("data:    %s", config.DATA_DIR)
    log.info("history: %s", ", ".join(config.history_sources()) or "none configured")

    db.init()

    # Said once, loudly, at boot. An unwritable music directory is the one
    # misconfiguration that leaves everything else looking healthy: the app
    # serves, scans and matches perfectly, and every download dies at the last
    # step having already spent the bandwidth and the encode.
    problem = config.music_dir_problem()
    if problem:
        log.error("music library is not writable — downloads will fail: %s", problem)

    # The same argument, for the runtime that solves YouTube's challenges: when
    # it is missing or too old nothing announces it, and the cost lands as 403s
    # on individual downloads hours later.
    problem = download.js_runtime_problem()
    if problem:
        log.error("youtube downloads will be degraded: %s", problem)

    # And for the TLS fingerprint, which decides whether YouTube answers the
    # media fetch at all from a VPN or a datacenter address.
    log.info("tls:     %s", download.impersonation_status())

    # One playlist, not one per scan. Installs that predate that have their old
    # per-scan files folded into it here, once.
    download.consolidate_scan_playlists()

    if not config.TESTING:
        download.start_workers()
        scheduler.start()
        reschedule()

    log.info("ready on http://%s:%d", config.HOST, config.PORT)
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        log.info("Musicdrome stopped")


app = FastAPI(
    title="Musicdrome",
    description="AI music discovery that downloads what it recommends.",
    version=__version__,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

app.include_router(router)

# Mounted last so /api keeps priority over the static index.
if config.STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="ui")
else:  # pragma: no cover - only reachable from a broken image

    @app.get("/")
    def missing_ui() -> JSONResponse:
        return JSONResponse(
            {"service": "musicdrome", "version": __version__,
             "message": f"No UI at {config.STATIC_DIR}. The API is at /api."},
            status_code=503,
        )
