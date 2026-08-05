"""Authentication dependencies for the native API."""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db, utcnow
from .models import User
from .security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    encrypt_secret,
    hash_password,
    needs_rehash,
    verify_password,
)

log = logging.getLogger(__name__)

bearer_scheme = HTTPBearer(auto_error=False)


class AuthError(HTTPException):
    def __init__(self, detail: str = "Not authenticated") -> None:
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


def _token_from_request(
    request: Request, credentials: HTTPAuthorizationCredentials | None
) -> str | None:
    """Bearer header first; fall back to ``?token=`` for <audio> and <img> tags,
    which cannot carry custom headers."""
    if credentials and credentials.scheme.lower() == "bearer":
        return credentials.credentials
    query_token = request.query_params.get("token")
    if query_token:
        return query_token
    return None


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    token = _token_from_request(request, credentials)
    if not token:
        raise AuthError()

    payload = decode_token(token, expected_type="access")
    if not payload:
        raise AuthError("Invalid or expired token")

    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        raise AuthError("Malformed token")

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise AuthError("User not found or disabled")
    return user


def get_current_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Administrator access required"
        )
    return user


def get_optional_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User | None:
    try:
        return get_current_user(request, credentials, db)
    except HTTPException:
        return None


# ─── User management helpers ───────────────────────────────────────────────


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.username == username))
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None

    # Opportunistically upgrade the hash and backfill the encrypted copy that
    # Subsonic token auth needs (older rows, or users created before the key
    # was configured).
    changed = False
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        changed = True
    if not user.password_enc:
        user.password_enc = encrypt_secret(password)
        changed = True

    user.last_login_at = utcnow()
    if changed:
        db.add(user)
    db.commit()
    return user


def issue_tokens(user: User) -> dict[str, object]:
    return {
        "access_token": create_access_token(user.id, user.username, user.is_admin),
        "refresh_token": create_refresh_token(user.id),
        "token_type": "bearer",
        "expires_in": settings.access_token_expire_minutes * 60,
    }


def create_user(
    db: Session,
    username: str,
    password: str,
    *,
    email: str | None = None,
    is_admin: bool = False,
    commit: bool = True,
) -> User:
    user = User(
        username=username,
        email=email,
        password_hash=hash_password(password),
        password_enc=encrypt_secret(password),
        is_admin=is_admin,
        is_active=True,
        # Admins get the permissions that gate destructive Subsonic verbs.
        download_role=True,
        upload_role=is_admin,
        playlist_role=True,
        cover_art_role=True,
        comment_role=is_admin,
        podcast_role=True,
        stream_role=True,
        share_role=is_admin,
    )
    db.add(user)
    if commit:
        db.commit()
        db.refresh(user)
    return user


def set_password(db: Session, user: User, password: str, *, commit: bool = True) -> None:
    user.password_hash = hash_password(password)
    user.password_enc = encrypt_secret(password)
    db.add(user)
    if commit:
        db.commit()
