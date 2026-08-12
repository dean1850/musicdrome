"""The HTTP API.

Small and JSON-only. The UI is a static page that polls a handful of these
endpoints; there is no authentication because Musicdrome is meant to sit on a
trusted home network, behind a VPN or a reverse proxy if it needs to be reached
from outside.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from . import ai, config, db, download, history, links, scan, stats, users
from .sources import navidrome

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

VALID_ACTIONS = {"save", "unsave", "hide", "unhide", "download"}


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/status")
def status(user_id: int | None = Query(None)) -> dict[str, Any]:
    """Everything the header and the setup hints need, in one request."""
    user_id = users.resolve(user_id)
    scope = "WHERE user_id = ?" if user_id else ""
    params = [user_id] if user_id else []

    with db.connect() as conn:
        counts = {
            row["status"]: row["n"]
            for row in conn.execute(
                f"SELECT status, COUNT(*) AS n FROM suggestions {scope} GROUP BY status", params
            )
        }
        last_scan = conn.execute(
            f"SELECT * FROM scans {scope} ORDER BY id DESC LIMIT 1", params
        ).fetchone()

    return {
        "ai": ai.status(),
        "history": history.status(user_id),
        "scan": {**scan.state(), "last": dict(last_scan) if last_scan else None},
        "counts": counts,
        "downloads_today": download.downloads_today(),
        "settings": db.get_settings(),
        "music_dir": str(config.MUSIC_DIR),
        "exclude_dir": config.EXCLUDE_MUSIC_DIR,
        # Empty unless the library cannot actually be written to. The UI shows
        # this as a banner, because it is the one misconfiguration that lets
        # everything else look healthy right up until a download finishes.
        "music_dir_problem": config.music_dir_problem(),
        "users": users.all_users(),
        "user_id": user_id,
        "navidrome": navidrome.configured(),
    }


# ─── Users ─────────────────────────────────────────────────────────────────


@router.get("/users")
def list_users() -> dict[str, Any]:
    return {"users": users.all_users(), "default_id": users.default_id()}


@router.post("/users")
def create_user(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return {"user": users.create(**body)}
    except users.UserError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.put("/users/{user_id}")
def update_user(user_id: int, updates: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return {"user": users.update(user_id, updates)}
    except users.UserError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/users/{user_id}")
def delete_user(user_id: int) -> dict[str, bool]:
    if not users.delete(user_id):
        raise HTTPException(404, "no such user")
    return {"deleted": True}


@router.post("/users/discover")
def discover_users() -> dict[str, Any]:
    """Import the household roster from Navidrome.

    Only names and mail addresses come across — Navidrome does not expose which
    Last.fm or ListenBrainz account a user has linked, so those still have to
    be filled in per person afterwards.
    """
    try:
        roster = navidrome.users()
    except navidrome.NavidromeError as exc:
        raise HTTPException(400, str(exc)) from exc

    result = users.import_roster(roster)
    return {**result, "users": users.all_users()}


@router.get("/users/navidrome")
def navidrome_status() -> dict[str, Any]:
    if not navidrome.configured():
        return {"configured": False}
    return {"configured": True, **navidrome.ping()}


# ─── Scanning ──────────────────────────────────────────────────────────────


@router.post("/scan")
def start_scan(user_id: int | None = Body(None, embed=True)) -> dict[str, Any]:
    """Scan for one user, or for everyone when no user is named."""
    if not scan.run_in_background("manual", users.resolve(user_id) if user_id else None):
        raise HTTPException(409, "a scan is already running, or no users are configured")
    return {"started": True}


@router.get("/scan")
def scan_status(user_id: int | None = Query(None)) -> dict[str, Any]:
    scope, params = ("WHERE user_id = ?", [user_id]) if user_id else ("", [])
    with db.connect() as conn:
        recent = [
            dict(row)
            for row in conn.execute(
                f"SELECT s.*, u.name AS user_name FROM scans s "
                f"LEFT JOIN users u ON u.id = s.user_id "
                f"{scope.replace('user_id', 's.user_id')} ORDER BY s.id DESC LIMIT 10",
                params,
            )
        ]
    return {"state": scan.state(), "recent": recent}


# ─── Suggestions ───────────────────────────────────────────────────────────


@router.get("/suggestions")
def list_suggestions(
    status: str = Query("new"),
    min_match: int = Query(0, ge=0, le=100),
    tag: str = Query(""),
    sort: str = Query(""),
    limit: int = Query(200, ge=1, le=1000),
    user_id: int | None = Query(None),
) -> dict[str, Any]:
    """Cards for the discover grid, plus the tag counts the filter bar shows."""
    order = {
        "match": "match DESC, created_at DESC",
        "newest": "created_at DESC, match DESC",
        "artist": "artist COLLATE NOCASE ASC, title COLLATE NOCASE ASC",
    }.get(sort or db.get_setting("sort"), "match DESC, created_at DESC")

    user_id = users.resolve(user_id)
    where = ["match >= ?"]
    params: list[Any] = [min_match]
    if user_id:
        where.append("user_id = ?")
        params.append(user_id)
    if status != "all":
        where.append("status = ?")
        params.append(status)

    # The tag filter narrows the cards but not the counts, so a selected tag
    # does not shrink every other count to zero.
    count_clause = " AND ".join(where)
    count_params = list(params)

    if tag:
        where.append("(',' || lower(tags) || ',') LIKE ?")
        params.append(f"%,{tag.lower()},%")

    clause = " AND ".join(where)
    with db.connect() as conn:
        rows = [
            _card(row)
            for row in conn.execute(
                f"SELECT * FROM suggestions WHERE {clause} ORDER BY {order} LIMIT ?",
                [*params, limit],
            )
        ]
        tag_rows = conn.execute(
            f"SELECT tags FROM suggestions WHERE {count_clause}", count_params
        ).fetchall()

    counts: dict[str, int] = {}
    for row in tag_rows:
        for name in (row["tags"] or "").split(","):
            name = name.strip().lower()
            if name:
                counts[name] = counts.get(name, 0) + 1

    return {
        "suggestions": rows,
        "tags": sorted(
            ({"name": name, "count": count} for name, count in counts.items()),
            key=lambda item: (-item["count"], item["name"]),
        )[:30],
    }


def _card(row) -> dict[str, Any]:
    card = dict(row)
    card["tags"] = [t for t in (card.get("tags") or "").split(",") if t]
    return card


@router.post("/suggestions/{suggestion_id}/{action}")
def act_on_suggestion(suggestion_id: int, action: str) -> dict[str, Any]:
    if action not in VALID_ACTIONS:
        raise HTTPException(400, f"unknown action '{action}'")

    with db.connect() as conn:
        row = conn.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such suggestion")

    if action == "download":
        download_id = download.enqueue(suggestion_id)
        if download_id is None:
            raise HTTPException(409, "could not queue this download")
        return {"queued": True, "download_id": download_id}

    new_status = {"save": "saved", "unsave": "new", "hide": "hidden", "unhide": "new"}[action]
    with db.connect() as conn:
        conn.execute(
            "UPDATE suggestions SET status = ?, decided_at = ? WHERE id = ?",
            (new_status, db.now(), suggestion_id),
        )
    return {"status": new_status}


@router.post("/suggestions/download-all")
def download_all(
    min_match: int = Body(0, embed=True), user_id: int | None = Body(None, embed=True)
) -> dict[str, int]:
    """Queue every new suggestion at or above a match percentage.

    Scoped to the selected user: "download all" from one person's grid must not
    quietly pull in everybody else's cards too.
    """
    user_id = users.resolve(user_id)
    scope, params = ("AND user_id = ?", [min_match, user_id]) if user_id else ("", [min_match])
    with db.connect() as conn:
        ids = [
            row["id"]
            for row in conn.execute(
                f"SELECT id FROM suggestions WHERE status = 'new' AND match >= ? {scope} "
                "ORDER BY match DESC",
                params,
            )
        ]
    return {"queued": sum(1 for suggestion_id in ids if download.enqueue(suggestion_id))}


# ─── Downloads ─────────────────────────────────────────────────────────────


@router.get("/downloads")
def list_downloads(
    status: str = Query("all"),
    limit: int = Query(200, ge=1, le=1000),
    user_id: int | None = Query(None),
    mine: bool = Query(False),
) -> dict[str, Any]:
    """The download list.

    Unfiltered by default, and deliberately so: the library is shared, so
    everyone should see what the household is pulling down. ``mine=true``
    narrows it to one person's requests.
    """
    where, params = [], []
    if status != "all":
        where.append("d.status = ?")
        params.append(status)
    if mine and (resolved := users.resolve(user_id)):
        where.append("d.user_id = ?")
        params.append(resolved)

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with db.connect() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT d.*, s.match, s.cover_url, s.year, u.name AS user_name FROM downloads d "
                "LEFT JOIN suggestions s ON s.id = d.suggestion_id "
                "LEFT JOIN users u ON u.id = d.user_id "
                f"{clause} ORDER BY d.created_at DESC, d.id DESC LIMIT ?",
                [*params, limit],
            )
        ]
    return {"downloads": rows, "active": download.active()}


@router.get("/downloads/active")
def active_downloads() -> dict[str, Any]:
    return {"active": download.active()}


@router.post("/downloads/url")
def download_from_url(
    url: str = Body("", embed=True), user_id: int | None = Body(None, embed=True)
) -> dict[str, Any]:
    """Queue a track from a pasted Spotify, YouTube Music or YouTube link."""
    try:
        resolved = links.resolve(url)
    except links.LinkError as exc:
        raise HTTPException(400, str(exc)) from exc

    if download.track_is_downloaded(resolved["artist"], resolved["title"]):
        raise HTTPException(409, f"{resolved['artist']} — {resolved['title']} is already downloaded")

    download_id = download.enqueue_direct(
        artist=resolved["artist"],
        title=resolved["title"],
        album=resolved.get("album", ""),
        url=resolved.get("url", ""),
        source=resolved.get("source", "url"),
        user_id=users.resolve(user_id),
    )
    return {
        "queued": True,
        "download_id": download_id,
        "artist": resolved["artist"],
        "title": resolved["title"],
        # A Spotify link carries no downloadable audio, so it still has to be
        # matched on YouTube Music — worth saying so in the UI.
        "matched": bool(resolved.get("url")),
    }


@router.post("/downloads/retry-failed")
def retry_failed_downloads() -> dict[str, int]:
    return {"queued": download.retry_all_failed()}


@router.post("/downloads/{download_id}/retry")
def retry_download(download_id: int) -> dict[str, bool]:
    if not download.retry(download_id):
        raise HTTPException(404, "no such download, or it is already running")
    return {"queued": True}


@router.delete("/downloads/{download_id}")
def delete_download(download_id: int, delete_file: bool = Query(False)) -> dict[str, bool]:
    if not download.remove(download_id, delete_file=delete_file):
        raise HTTPException(404, "no such download")
    return {"deleted": True}


# ─── Stats ─────────────────────────────────────────────────────────────────


@router.get("/stats")
def get_stats(
    days: int = Query(90, ge=1, le=3650), user_id: int | None = Query(None)
) -> dict[str, Any]:
    return stats.overview(days, user_id=users.resolve(user_id))


@router.get("/stats/summary")
def get_summary(
    days: int = Query(90, ge=1, le=3650),
    refresh: bool = Query(False),
    user_id: int | None = Query(None),
) -> dict[str, Any]:
    return stats.taste_summary(days=days, force=refresh, user_id=users.resolve(user_id))


# ─── Settings ──────────────────────────────────────────────────────────────


@router.get("/settings")
def get_settings() -> dict[str, Any]:
    return {"settings": db.get_settings(), "defaults": db.DEFAULT_SETTINGS}


@router.put("/settings")
def put_settings(updates: dict[str, Any] = Body(...)) -> dict[str, Any]:
    saved = db.save_settings(updates)
    from .main import reschedule

    reschedule()
    return {"settings": saved}
