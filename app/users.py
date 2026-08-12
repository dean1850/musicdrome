"""The people in the household.

A user here is a taste profile, not an account. There is no password and no
session: Musicdrome is meant to sit on a trusted home network, and adding a
login would buy nothing except somewhere for a password to leak from. The UI
asks who you are the same way a streaming box does, and that choice only ever
decides whose suggestions you are looking at.

What is genuinely per-user is the listening history behind those suggestions —
each person brings their own Last.fm and ListenBrainz identities. What is
deliberately shared is the music itself: one library on disk, one download
queue, because a household has one record collection.

Rosters can be imported from Navidrome, which knows who has an account on the
server. It does not know who those people are on Last.fm — Subsonic's getUsers
returns names, mail addresses and roles and never exposes a linked scrobble
account — so the names arrive automatically and the scrobble usernames are
filled in once, by hand, in Settings.
"""

from __future__ import annotations

import logging
from typing import Any

from . import db

log = logging.getLogger(__name__)

# Columns a caller is allowed to write. `source` and `created_at` are ours.
EDITABLE = (
    "name",
    "email",
    "lastfm_user",
    "listenbrainz_user",
    "listenbrainz_token",
    "active",
)


class UserError(RuntimeError):
    pass


def _row(row) -> dict[str, Any]:
    user = dict(row)
    user["active"] = bool(user.get("active", 1))
    # Never hand a token back to the browser; the UI only needs to know that
    # one is set so it can show the field as filled.
    user["has_listenbrainz_token"] = bool(user.pop("listenbrainz_token", ""))
    return user


def all_users(include_inactive: bool = True) -> list[dict[str, Any]]:
    clause = "" if include_inactive else "WHERE active = 1"
    with db.connect() as conn:
        return [
            _row(row)
            for row in conn.execute(f"SELECT * FROM users {clause} ORDER BY id")
        ]


def active_users() -> list[dict[str, Any]]:
    """Everyone a scheduled scan should run for."""
    return all_users(include_inactive=False)


def get(user_id: int) -> dict[str, Any] | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row(row) if row else None


def credentials(user_id: int) -> dict[str, str]:
    """The scrobble identities for one user, token included.

    Kept apart from :func:`get` so the token has exactly one way out of the
    database, and it is not the one the API serialises.
    """
    with db.connect() as conn:
        row = conn.execute(
            "SELECT lastfm_user, listenbrainz_user, listenbrainz_token FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else {}


def default_id() -> int | None:
    """Whose grid to show someone who has not picked a user yet."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE active = 1 ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:  # everyone deactivated — fall back to anyone at all
            row = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    return int(row["id"]) if row else None


def resolve(user_id: int | None) -> int | None:
    """Validate a user id from a request, falling back to the default.

    A stale id in a bookmarked URL or a browser's saved state must not 404 the
    whole page, so an unknown id quietly becomes the default user.
    """
    if user_id and get(user_id):
        return user_id
    return default_id()


def create(**fields: Any) -> dict[str, Any]:
    name = str(fields.get("name", "")).strip()
    if not name:
        raise UserError("a user needs a name")

    values = {key: fields.get(key, "") for key in EDITABLE}
    values["name"] = name
    values["active"] = 1 if fields.get("active", True) else 0

    with db.connect() as conn:
        clash = conn.execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
        if clash:
            raise UserError(f"there is already a user called {name}")
        cursor = conn.execute(
            "INSERT INTO users (name, email, lastfm_user, listenbrainz_user, "
            "listenbrainz_token, source, active, created_at) "
            "VALUES (:name, :email, :lastfm_user, :listenbrainz_user, "
            ":listenbrainz_token, :source, :active, :created_at)",
            {
                **values,
                "source": fields.get("source", "manual"),
                "created_at": db.now(),
            },
        )
        user_id = int(cursor.lastrowid)

    log.info("added user '%s'", name)
    return get(user_id) or {}


def update(user_id: int, updates: dict[str, Any]) -> dict[str, Any]:
    if get(user_id) is None:
        raise UserError("no such user")

    clean: dict[str, Any] = {}
    for key, value in updates.items():
        if key not in EDITABLE:
            continue
        if key == "active":
            clean[key] = 1 if value else 0
        elif key == "listenbrainz_token" and value == "":
            continue  # blank means "leave it alone", not "clear it"
        else:
            clean[key] = str(value).strip()

    if "name" in clean:
        if not clean["name"]:
            raise UserError("a user needs a name")
        with db.connect() as conn:
            clash = conn.execute(
                "SELECT id FROM users WHERE name = ? AND id != ?", (clean["name"], user_id)
            ).fetchone()
        if clash:
            raise UserError(f"there is already a user called {clean['name']}")

    if clean:
        assignments = ", ".join(f"{key} = :{key}" for key in clean)
        with db.connect() as conn:
            conn.execute(
                f"UPDATE users SET {assignments} WHERE id = :id", {**clean, "id": user_id}
            )
    return get(user_id) or {}


def delete(user_id: int) -> bool:
    """Remove a user, and with them their history and suggestions.

    Downloads survive: the files are on disk and shared with the household, so
    orphaning the rows would misreport the library. Their ``user_id`` goes null
    through the foreign key instead.
    """
    with db.connect() as conn:
        row = conn.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    log.info("removed user '%s'", row["name"])
    return True


def import_roster(people: list[dict[str, Any]], source: str = "navidrome") -> dict[str, Any]:
    """Add users discovered on another server, leaving existing ones alone.

    Matching is by name. Somebody already in the database keeps whatever
    scrobble usernames have been filled in for them — re-importing a roster is
    meant to pick up new housemates, not to undo configuration.
    """
    added, skipped = [], []
    existing = {user["name"].casefold() for user in all_users()}

    for person in people:
        name = str(person.get("name", "")).strip()
        if not name:
            continue
        if name.casefold() in existing:
            skipped.append(name)
            continue
        try:
            create(name=name, email=person.get("email", ""), source=source)
            existing.add(name.casefold())
            added.append(name)
        except UserError as exc:
            log.warning("could not import user %s: %s", name, exc)
            skipped.append(name)

    return {"added": added, "skipped": skipped}
