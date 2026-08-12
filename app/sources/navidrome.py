"""Navidrome — who has an account on the music server.

Read-only, and used for exactly one thing: saving a household from typing its
own members in. Navidrome speaks the Subsonic API, whose ``getUsers`` returns
every account on the server.

**What this cannot do.** ``getUsers`` returns a username, a mail address and a
pile of role flags. It does not return the Last.fm or ListenBrainz account that
user has linked — Navidrome keeps each user's scrobble session private and
never exposes it over the API, not even to an admin. So this discovers *who* is
in the house, and each person's scrobble usernames are still entered once by
hand. There is no way around that short of reading Navidrome's database
directly, which would be both fragile and rude.

Listing users is admin-only, so the credentials in the environment have to
belong to an admin. Authentication uses Subsonic's salted token scheme rather
than sending the password itself.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from typing import Any

import httpx

from .. import config

log = logging.getLogger(__name__)

TIMEOUT = 15.0
# The version floor for the token auth scheme used below.
API_VERSION = "1.13.0"
CLIENT_NAME = "musicdrome"


class NavidromeError(RuntimeError):
    pass


def configured() -> bool:
    return bool(config.NAVIDROME_URL and config.NAVIDROME_USER and config.NAVIDROME_PASSWORD)


def _auth_params() -> dict[str, str]:
    """Subsonic's salted-token auth: md5(password + salt), never the password."""
    salt = secrets.token_hex(8)
    token = hashlib.md5(f"{config.NAVIDROME_PASSWORD}{salt}".encode()).hexdigest()
    return {
        "u": config.NAVIDROME_USER,
        "t": token,
        "s": salt,
        "v": API_VERSION,
        "c": CLIENT_NAME,
        "f": "json",
    }


def _get(endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    if not configured():
        raise NavidromeError("NAVIDROME_URL, NAVIDROME_USER and NAVIDROME_PASSWORD must all be set")

    url = f"{config.NAVIDROME_URL}/rest/{endpoint}"
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
            response = client.get(url, params={**_auth_params(), **(params or {})})
    except httpx.HTTPError as exc:
        raise NavidromeError(f"could not reach Navidrome at {config.NAVIDROME_URL}: {exc}") from exc

    if response.status_code >= 400:
        raise NavidromeError(f"HTTP {response.status_code} from Navidrome")

    try:
        body = response.json().get("subsonic-response") or {}
    except ValueError as exc:
        raise NavidromeError("Navidrome did not return JSON — is the URL right?") from exc

    if body.get("status") == "failed":
        error = body.get("error") or {}
        code, message = error.get("code"), error.get("message", "")
        if code == 40:
            raise NavidromeError("Navidrome rejected the username or password")
        if code == 50:
            raise NavidromeError(
                "that Navidrome account is not an admin — listing users requires one"
            )
        raise NavidromeError(f"Navidrome error {code}: {message}")

    return body


def users() -> list[dict[str, str]]:
    """Every account on the Navidrome server, as ``{"name", "email"}``."""
    body = _get("getUsers.view")
    entries = (body.get("users") or {}).get("user") or []
    if isinstance(entries, dict):  # a one-user server returns an object
        entries = [entries]

    people = []
    for entry in entries:
        name = (entry.get("username") or "").strip()
        if name:
            people.append({"name": name, "email": (entry.get("email") or "").strip()})

    log.info("Navidrome returned %d user(s)", len(people))
    return people


def ping() -> dict[str, Any]:
    """Check the URL and credentials without importing anything."""
    try:
        body = _get("ping.view")
    except NavidromeError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "version": body.get("version", "")}
