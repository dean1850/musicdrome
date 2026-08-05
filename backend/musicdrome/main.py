"""FastAPI application entry point."""

from __future__ import annotations

import logging
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from . import __version__
from .api import router as api_router
from .auth import create_user
from .config import settings
from .db import init_db, session_scope
from .models import User
from .services import jobs
from .services.smartplaylist import seed_default_playlists
from .services.watcher import watcher
from .subsonic import router as subsonic_router
from .subsonic.common import SubsonicError, render

log = logging.getLogger(__name__)


def configure_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    # These libraries are chatty at INFO and drown out our own logs
    for noisy in ("httpx", "httpcore", "apscheduler.executors.default", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def bootstrap_admin() -> None:
    """Create the first administrator if the instance has no users yet."""
    with session_scope() as db:
        if (db.scalar(select(func.count(User.id))) or 0) > 0:
            return

        username = settings.default_admin_username or "admin"
        password = settings.default_admin_password or secrets.token_urlsafe(12)
        generated = not settings.default_admin_password

        user = create_user(db, username, password, is_admin=True)
        seed_default_playlists(db, user)

        if generated:
            log.warning(
                "\n"
                "  ┌──────────────────────────────────────────────────────────┐\n"
                "  │  Musicdrome created its first administrator account.     │\n"
                "  │                                                          │\n"
                f"  │    username: {username:<43} │\n"
                f"  │    password: {password:<43} │\n"
                "  │                                                          │\n"
                "  │  Change it after signing in, or set DEFAULT_ADMIN_*      │\n"
                "  │  in .env before the first start.                         │\n"
                "  └──────────────────────────────────────────────────────────┘"
            )
        else:
            log.info("created administrator '%s' from DEFAULT_ADMIN_* settings", username)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    log.info("Musicdrome %s starting", __version__)
    log.info("music: %s", settings.music_dir)
    log.info("data:  %s", settings.data_dir)

    init_db()
    bootstrap_admin()

    if settings.scan_on_startup and not settings.testing:
        import threading

        from .services import scanner

        threading.Thread(
            target=scanner.scan_library, kwargs={"full": False}, daemon=True
        ).start()

    if not settings.testing:
        watcher.start()
        jobs.start()

    log.info("ready on http://%s:%s", settings.host, settings.port)
    try:
        yield
    finally:
        jobs.shutdown()
        watcher.stop()
        log.info("Musicdrome stopped")


app = FastAPI(
    title="Musicdrome",
    description=(
        "A self-hosted music server with a Subsonic-compatible API, smart and "
        "AI-curated playlists, scrobbling, podcasts and library acquisition."
    ),
    version=__version__,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
)


@app.exception_handler(SubsonicError)
async def subsonic_error_handler(request: Request, exc: SubsonicError):
    """Subsonic reports every failure as HTTP 200 with an error document."""
    fmt = request.query_params.get("f", "xml")
    callback = request.query_params.get("callback")
    return render(fmt=fmt, callback=callback, error=exc)


app.include_router(api_router)
app.include_router(subsonic_router)


# ─── Frontend ──────────────────────────────────────────────────────────────

_static_dir = Path(settings.static_dir)

if _static_dir.is_dir():
    assets = _static_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        """Serve the built SPA, letting the client router own unknown paths."""
        candidate = (_static_dir / full_path).resolve()
        try:
            candidate.relative_to(_static_dir.resolve())
        except ValueError:
            candidate = _static_dir / "index.html"  # path traversal attempt

        if full_path and candidate.is_file():
            return FileResponse(candidate)

        index = _static_dir / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse(
            {"detail": "Frontend not built. Run `npm run build` in frontend/."},
            status_code=503,
        )

else:

    @app.get("/", include_in_schema=False)
    async def no_frontend():
        return JSONResponse(
            {
                "service": "musicdrome",
                "version": __version__,
                "message": (
                    f"No built frontend at {_static_dir}. The API is available at "
                    "/api/v1 and the Subsonic API at /rest."
                ),
            }
        )
