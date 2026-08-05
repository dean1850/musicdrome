"""Native REST API consumed by the Musicdrome web UI."""

from fastapi import APIRouter

from .routes_admin import router as admin_router
from .routes_auth import router as auth_router
from .routes_discovery import router as discovery_router
from .routes_library import router as library_router
from .routes_playlists import router as playlists_router
from .routes_podcasts import router as podcasts_router

router = APIRouter(prefix="/api/v1")
router.include_router(auth_router)
router.include_router(library_router)
router.include_router(playlists_router)
router.include_router(discovery_router)
router.include_router(podcasts_router)
router.include_router(admin_router)

__all__ = ["router"]
