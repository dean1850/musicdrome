"""Password hashing, credential encryption and JWT issuance.

Two password representations are kept per user, deliberately:

* an **argon2id hash** used for web login — one-way, the only thing consulted
  for the native API;
* a **Fernet-encrypted copy** used for Subsonic ``token``/``salt`` auth, which
  is defined as ``md5(password + salt)`` and therefore requires the server to
  recover the cleartext. Navidrome makes the same trade-off; the encryption key
  lives outside the database in ``CREDENTIAL_ENCRYPTION_KEY``.

Set ``SUBSONIC_REQUIRE_TOKEN_AUTH=true`` to reject cleartext ``?p=`` logins.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from .config import settings

log = logging.getLogger(__name__)

_hasher = PasswordHasher()


# ─── Fernet key derivation ─────────────────────────────────────────────────


def _build_fernet() -> Fernet:
    key = settings.credential_encryption_key.strip()
    if key:
        try:
            return Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError):
            log.error(
                "CREDENTIAL_ENCRYPTION_KEY is not a valid Fernet key; "
                "falling back to a key derived from SECRET_KEY. Generate one with: "
                "python3 -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            )
    derived = base64.urlsafe_b64encode(
        hashlib.sha256(f"musicdrome-credentials:{settings.secret_key}".encode()).digest()
    )
    return Fernet(derived)


_fernet = _build_fernet()


def encrypt_secret(value: str | None) -> str | None:
    """Encrypt a credential for storage. ``None``/empty passes through."""
    if not value:
        return None
    return _fernet.encrypt(value.encode()).decode()


def decrypt_secret(value: str | None) -> str | None:
    """Decrypt a stored credential, returning ``None`` if it cannot be read
    (usually because ``CREDENTIAL_ENCRYPTION_KEY`` changed)."""
    if not value:
        return None
    try:
        return _fernet.decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        log.warning("stored credential could not be decrypted — key may have changed")
        return None


# ─── Passwords ─────────────────────────────────────────────────────────────


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def generate_password(length: int = 16) -> str:
    return secrets.token_urlsafe(length)[:length]


# ─── Subsonic token auth ───────────────────────────────────────────────────


def subsonic_token(password: str, salt: str) -> str:
    """The Subsonic auth token: ``md5(password + salt)``, lowercase hex."""
    return hashlib.md5(f"{password}{salt}".encode("utf-8")).hexdigest()


def verify_subsonic_token(password: str, salt: str, token: str) -> bool:
    return secrets.compare_digest(subsonic_token(password, salt), (token or "").lower())


def decode_subsonic_password(value: str) -> str:
    """Subsonic clients may hex-encode the password as ``enc:<hex>``."""
    if value.startswith("enc:"):
        try:
            return bytes.fromhex(value[4:]).decode("utf-8", errors="replace")
        except ValueError:
            return value
    return value


# ─── JWT ───────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_access_token(user_id: int, username: str, is_admin: bool) -> str:
    expires = _now() + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": str(user_id),
        "username": username,
        "admin": is_admin,
        "type": "access",
        "iat": int(_now().timestamp()),
        "exp": int(expires.timestamp()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: int) -> str:
    expires = _now() + timedelta(days=settings.refresh_token_expire_days)
    payload = {
        "sub": str(user_id),
        "type": "refresh",
        "iat": int(_now().timestamp()),
        "exp": int(expires.timestamp()),
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str, expected_type: str = "access") -> dict[str, Any] | None:
    try:
        payload = jwt.decode(
            token, settings.secret_key, algorithms=[settings.jwt_algorithm]
        )
    except jwt.PyJWTError:
        return None
    if payload.get("type") != expected_type:
        return None
    return payload
