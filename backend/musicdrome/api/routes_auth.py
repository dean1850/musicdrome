"""Authentication and account endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import authenticate, create_user, get_current_user, issue_tokens, set_password
from ..config import settings
from ..db import get_db
from ..models import User
from ..security import create_access_token, create_refresh_token, decode_token, verify_password
from ..services.lastfm import LastFmError
from ..services.listenbrainz import ListenBrainzError
from ..services.scrobble import link_lastfm, link_listenbrainz
from ..services.smartplaylist import seed_default_playlists
from .schemas import (
    GenericResponse,
    LastFmLinkRequest,
    ListenBrainzLinkRequest,
    LoginRequest,
    PasswordChangeRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserOut,
    UserSettingsRequest,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


@router.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = authenticate(db, payload.username, payload.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )
    return issue_tokens(user)


@router.post("/auth/register", response_model=TokenResponse)
def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    """Self-registration. Disabled unless ALLOW_OPEN_REGISTRATION=true."""
    first_user = (db.scalar(select(func.count(User.id))) or 0) == 0

    if not settings.allow_open_registration and not first_user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Registration is closed — ask an administrator for an account",
        )
    if db.scalar(select(User).where(User.username == payload.username)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="That username is taken"
        )

    user = create_user(
        db,
        payload.username,
        payload.password,
        email=payload.email,
        # The very first account is always the administrator
        is_admin=first_user,
    )
    seed_default_playlists(db, user)
    return issue_tokens(user)


@router.post("/auth/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)):
    claims = decode_token(payload.refresh_token, expected_type="refresh")
    if not claims:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    user = db.get(User, int(claims["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or disabled"
        )

    return {
        "access_token": create_access_token(user.id, user.username, user.is_admin),
        "refresh_token": create_refresh_token(user.id),
        "token_type": "bearer",
        "expires_in": settings.access_token_expire_minutes * 60,
    }


@router.get("/auth/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.put("/auth/me", response_model=UserOut)
def update_me(
    payload: UserSettingsRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if payload.max_bitrate is not None:
        user.max_bitrate = payload.max_bitrate
    if payload.transcode_format is not None:
        user.transcode_format = payload.transcode_format or None
    if payload.ai_enabled is not None:
        user.ai_enabled = payload.ai_enabled
    if payload.lastfm_enabled is not None:
        user.lastfm_enabled = payload.lastfm_enabled
    if payload.listenbrainz_enabled is not None:
        user.listenbrainz_enabled = payload.listenbrainz_enabled

    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/auth/password", response_model=GenericResponse)
def change_password(
    payload: PasswordChangeRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not payload.current_password or not verify_password(
        payload.current_password, user.password_hash
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect"
        )

    set_password(db, user, payload.new_password)
    return GenericResponse(message="Password updated")


# ─── Scrobbling links ──────────────────────────────────────────────────────


@router.post("/auth/lastfm", response_model=GenericResponse)
def connect_lastfm(
    payload: LastFmLinkRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not settings.lastfm_api_key or not settings.lastfm_api_secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Set LASTFM_API_KEY and LASTFM_API_SECRET in .env first",
        )
    try:
        username = link_lastfm(db, user, payload.username, payload.password)
    except LastFmError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return GenericResponse(message=f"Connected to Last.fm as {username}")


@router.delete("/auth/lastfm", response_model=GenericResponse)
def disconnect_lastfm(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    user.lastfm_enabled = False
    user.lastfm_session_key = None
    user.lastfm_username = None
    db.add(user)
    db.commit()
    return GenericResponse(message="Disconnected from Last.fm")


@router.post("/auth/listenbrainz", response_model=GenericResponse)
def connect_listenbrainz(
    payload: ListenBrainzLinkRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        username = link_listenbrainz(db, user, payload.token)
    except ListenBrainzError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return GenericResponse(message=f"Connected to ListenBrainz as {username}")


@router.delete("/auth/listenbrainz", response_model=GenericResponse)
def disconnect_listenbrainz(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    user.listenbrainz_enabled = False
    user.listenbrainz_token = None
    db.add(user)
    db.commit()
    return GenericResponse(message="Disconnected from ListenBrainz")
