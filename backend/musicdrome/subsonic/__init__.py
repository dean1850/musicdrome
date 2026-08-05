"""Subsonic API v1.16.1 — the compatibility layer every Subsonic client speaks."""

from fastapi import APIRouter

from .routes_browsing import router as browsing_router
from .routes_media import router as media_router
from .routes_playlists import router as playlists_router
from .routes_system import router as system_router

router = APIRouter(prefix="/rest", tags=["subsonic"])
router.include_router(system_router)
router.include_router(browsing_router)
router.include_router(media_router)
router.include_router(playlists_router)

__all__ = ["router"]
