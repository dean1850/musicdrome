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

from . import ai, config, db, download, history, links, scan, stats

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

VALID_ACTIONS = {"save", "unsave", "hide", "unhide", "download"}


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/status")
def status() -> dict[str, Any]:
    """Everything the header and the setup hints need, in one request."""
    with db.connect() as conn:
        counts = {
            row["status"]: row["n"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM suggestions GROUP BY status"
            )
        }
        last_scan = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()

    return {
        "ai": ai.status(),
        "history": history.status(),
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
        "playlist": str(config.PLAYLIST_PATH),
    }


# ─── Scanning ──────────────────────────────────────────────────────────────


@router.post("/scan")
def start_scan() -> dict[str, Any]:
    if not scan.run_in_background("manual"):
        raise HTTPException(409, "a scan is already running")
    return {"started": True}


@router.get("/scan")
def scan_status() -> dict[str, Any]:
    with db.connect() as conn:
        recent = [
            dict(row)
            for row in conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 10")
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
) -> dict[str, Any]:
    """Cards for the discover grid, plus the tag counts the filter bar shows."""
    order = {
        "match": "match DESC, created_at DESC",
        "newest": "created_at DESC, match DESC",
        "artist": "artist COLLATE NOCASE ASC, title COLLATE NOCASE ASC",
    }.get(sort or db.get_setting("sort"), "match DESC, created_at DESC")

    where = ["match >= ?"]
    params: list[Any] = [min_match]
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
def download_all(min_match: int = Body(0, embed=True)) -> dict[str, int]:
    """Queue every new suggestion at or above a match percentage."""
    with db.connect() as conn:
        ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM suggestions WHERE status = 'new' AND match >= ? "
                "ORDER BY match DESC",
                (min_match,),
            )
        ]
    return {"queued": sum(1 for suggestion_id in ids if download.enqueue(suggestion_id))}


# ─── Downloads ─────────────────────────────────────────────────────────────


@router.get("/downloads")
def list_downloads(
    status: str = Query("all"),
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    """The download list, newest first."""
    clause, params = ("WHERE d.status = ?", [status]) if status != "all" else ("", [])
    with db.connect() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT d.*, s.match, s.cover_url, s.year FROM downloads d "
                "LEFT JOIN suggestions s ON s.id = d.suggestion_id "
                f"{clause} ORDER BY d.created_at DESC, d.id DESC LIMIT ?",
                [*params, limit],
            )
        ]
    return {"downloads": rows, "active": download.active()}


@router.get("/downloads/active")
def active_downloads() -> dict[str, Any]:
    return {"active": download.active()}


@router.post("/downloads/url")
def download_from_url(url: str = Body("", embed=True)) -> dict[str, Any]:
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


@router.post("/downloads/delete")
def delete_downloads(
    ids: list[int] = Body(..., embed=True),
    delete_file: bool = Body(True, embed=True),
) -> dict[str, int]:
    """Remove a batch of downloads in one request.

    Declared above the ``/downloads/{download_id}`` routes because FastAPI
    matches in order, and a literal segment registered after a path parameter
    is never reached.

    The cap is a sanity bound rather than a policy: the list endpoint returns
    at most a thousand rows, so nothing the UI can legitimately select comes
    anywhere near it.
    """
    if not ids:
        raise HTTPException(400, "no downloads selected")
    if len(ids) > 1000:
        raise HTTPException(400, "too many downloads in one request")
    return download.remove_many(ids, delete_file=delete_file)


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
def get_stats(days: int = Query(90, ge=1, le=3650)) -> dict[str, Any]:
    return stats.overview(days)


@router.get("/stats/summary")
def get_summary(
    days: int = Query(90, ge=1, le=3650), refresh: bool = Query(False)
) -> dict[str, Any]:
    return stats.taste_summary(days=days, force=refresh)


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
