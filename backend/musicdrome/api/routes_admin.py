"""Administration: users, scanning, scheduled jobs and integration health."""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import create_user, get_current_admin, get_current_user, set_password
from ..config import settings
from ..db import get_db
from ..models import ScanRun, User
from ..services import jobs, scanner
from ..services.lidarr import LidarrError, lidarr
from ..services.smartplaylist import seed_default_playlists
from .schemas import (
    GenericResponse,
    ScanRequest,
    UserCreateRequest,
    UserOut,
    UserUpdateRequest,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])


# ─── Health ────────────────────────────────────────────────────────────────


@router.get("/health")
def health():
    """Unauthenticated liveness probe used by the container healthcheck."""
    return {"status": "ok", "service": "musicdrome"}


@router.get("/server-info")
def server_info(user: User = Depends(get_current_user)):
    from .. import __subsonic_version__, __version__

    return {
        "name": settings.server_name,
        "version": __version__,
        "subsonic_version": __subsonic_version__,
        "features": {
            "transcoding": settings.transcoding_enabled,
            "ai": settings.ai_enabled,
            "ai_provider": settings.ai_provider,
            "smart_playlists": settings.smart_playlist_enabled,
            "podcasts": settings.podcast_enabled,
            "lidarr": settings.lidarr_enabled,
            "acquisition": settings.acquisition_enabled,
            "auto_download": settings.auto_download,
            "recommendations": settings.recommendations_enabled,
            "lastfm": settings.lastfm_enabled and bool(settings.lastfm_api_key),
            "listenbrainz": settings.listenbrainz_enabled,
            "open_registration": settings.allow_open_registration,
        },
    }


# ─── Users ─────────────────────────────────────────────────────────────────


@router.get("/admin/users", response_model=list[UserOut])
def list_users(admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    return db.scalars(select(User).order_by(User.username)).all()


@router.post("/admin/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def add_user(
    payload: UserCreateRequest,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    if db.scalar(select(User).where(User.username == payload.username)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="That username is taken"
        )

    user = create_user(
        db,
        payload.username,
        payload.password,
        email=payload.email,
        is_admin=payload.is_admin,
    )
    seed_default_playlists(db, user)
    return user


@router.put("/admin/users/{user_id}", response_model=UserOut)
def edit_user(
    user_id: int,
    payload: UserUpdateRequest,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if payload.password:
        set_password(db, user, payload.password, commit=False)

    for field in (
        "email", "is_admin", "is_active", "max_bitrate", "transcode_format",
        "ai_enabled", "download_role", "upload_role", "playlist_role",
        "podcast_role", "share_role",
    ):
        value = getattr(payload, field)
        if value is not None:
            setattr(user, field, value)

    # Never let the last administrator demote or disable themselves out of access
    if user.id == admin.id and (payload.is_admin is False or payload.is_active is False):
        remaining = db.scalar(
            select(User).where(
                User.is_admin.is_(True), User.is_active.is_(True), User.id != user.id
            )
        )
        if remaining is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You are the only administrator — promote someone else first",
            )

    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.delete("/admin/users/{user_id}", response_model=GenericResponse)
def remove_user(
    user_id: int,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot delete your own account",
        )

    db.delete(user)
    db.commit()
    return GenericResponse(message=f"Deleted {user.username}")


# ─── Scanning ──────────────────────────────────────────────────────────────


@router.get("/admin/scan")
def scan_status(admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    last = db.scalar(select(ScanRun).order_by(ScanRun.started_at.desc()).limit(1))
    return {
        "state": scanner.scan_state(),
        "last_run": {
            "started_at": last.started_at.isoformat() if last else None,
            "finished_at": last.finished_at.isoformat() if last and last.finished_at else None,
            "tracks_seen": last.tracks_seen if last else 0,
            "tracks_added": last.tracks_added if last else 0,
            "tracks_updated": last.tracks_updated if last else 0,
            "tracks_removed": last.tracks_removed if last else 0,
            "error": last.error if last else None,
        }
        if last
        else None,
        "music_dir": str(settings.music_dir),
    }


@router.post("/admin/scan", response_model=GenericResponse)
def start_scan(payload: ScanRequest, admin: User = Depends(get_current_admin)):
    if scanner.is_scanning():
        return GenericResponse(ok=False, message="A scan is already running")

    threading.Thread(
        target=scanner.scan_library, kwargs={"full": payload.full}, daemon=True
    ).start()
    return GenericResponse(
        message="Full rescan started" if payload.full else "Scan started"
    )


# ─── Scheduled jobs ────────────────────────────────────────────────────────


@router.get("/admin/jobs")
def list_jobs(admin: User = Depends(get_current_admin)):
    return jobs.job_status()


@router.post("/admin/jobs/{job_id}/run", response_model=GenericResponse)
def run_job(job_id: str, admin: User = Depends(get_current_admin)):
    if not jobs.run_now(job_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such job")
    return GenericResponse(message=f"Job '{job_id}' scheduled to run now")


# ─── Integrations ──────────────────────────────────────────────────────────


@router.get("/admin/integrations")
def integrations(admin: User = Depends(get_current_admin)):
    from ..services.ai.provider import provider_status
    from ..services.transcode import ffmpeg_available

    return {
        "ffmpeg": {
            "available": ffmpeg_available(),
            "path": settings.ffmpeg_path,
            "transcoding_enabled": settings.transcoding_enabled,
        },
        "lastfm": {
            "enabled": settings.lastfm_enabled,
            "configured": bool(settings.lastfm_api_key),
            "can_scrobble": bool(settings.lastfm_api_key and settings.lastfm_api_secret),
        },
        "listenbrainz": {
            "enabled": settings.listenbrainz_enabled,
            "api_url": settings.listenbrainz_api_url,
        },
        "musicbrainz": {
            "enabled": settings.musicbrainz_enabled,
            "rate_limit": settings.musicbrainz_rate_limit,
        },
        "ai": provider_status(),
        "lidarr": {
            "enabled": settings.lidarr_enabled,
            "url": settings.lidarr_url,
            "configured": lidarr.configured,
        },
        "acquisition": {
            "enabled": settings.acquisition_enabled,
            "auto_download": settings.auto_download,
            "max_per_day": settings.acquisition_max_per_day,
        },
    }


@router.post("/admin/integrations/lidarr/test")
def test_lidarr(admin: User = Depends(get_current_admin)):
    if not lidarr.configured:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Set LIDARR_ENABLED=true, LIDARR_URL and LIDARR_API_KEY in .env",
        )
    try:
        result = lidarr.test_connection()
        result["root_folders"] = [f.get("path") for f in lidarr.root_folders()]
        result["quality_profiles"] = [
            {"id": p.get("id"), "name": p.get("name")} for p in lidarr.quality_profiles()
        ]
    except LidarrError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    return result


@router.post("/admin/integrations/lidarr/sync", response_model=GenericResponse)
def sync_lidarr(admin: User = Depends(get_current_admin)):
    if not lidarr.configured:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Lidarr is not configured"
        )
    from ..services import lidarr as lidarr_service

    result = lidarr_service.sync()
    return GenericResponse(message="Lidarr sync complete", data=result)
