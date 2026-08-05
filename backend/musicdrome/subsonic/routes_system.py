"""Subsonic system, user, queue and bookmark endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from ..auth import create_user as create_local_user
from ..auth import set_password
from ..config import settings
from ..db import utcnow
from ..models import (
    Bookmark,
    InternetRadioStation,
    PlayQueue,
    ScanRun,
    Share,
    Track,
    User,
)
from ..services import scanner
from ..services.smartplaylist import seed_default_playlists
from .common import (
    OPEN_SUBSONIC_EXTENSIONS,
    TRACK,
    SubsonicContext,
    SubsonicError,
    endpoint,
    get_context,
    make_id,
    now_playing,
    param_bool,
    param_int,
    params_of,
    parse_typed_id,
    track_dict,
    tracks_payload,
    user_dict,
)

log = logging.getLogger(__name__)

router = APIRouter()


def _require_admin(ctx: SubsonicContext) -> None:
    if not ctx.user.is_admin:
        raise SubsonicError(
            SubsonicError.NOT_AUTHORIZED,
            "User is not authorized for the given operation",
        )


# ─── System ────────────────────────────────────────────────────────────────


@endpoint(router, "ping")
def ping(ctx: SubsonicContext = Depends(get_context)):
    return ctx.ok()


@endpoint(router, "getLicense")
def get_license(ctx: SubsonicContext = Depends(get_context)):
    return ctx.ok(
        {
            "license": {
                "valid": True,
                "email": ctx.user.email or "",
                "licenseExpires": "2099-12-31T00:00:00.000Z",
            }
        }
    )


@endpoint(router, "getOpenSubsonicExtensions")
def get_open_subsonic_extensions(ctx: SubsonicContext = Depends(get_context)):
    return ctx.ok({"openSubsonicExtensions": OPEN_SUBSONIC_EXTENSIONS})


@endpoint(router, "getMusicFolders")
def get_music_folders(ctx: SubsonicContext = Depends(get_context)):
    return ctx.ok(
        {"musicFolders": {"musicFolder": [{"id": 0, "name": "Music"}]}}
    )


@endpoint(router, "getScanStatus")
def get_scan_status(ctx: SubsonicContext = Depends(get_context)):
    state = scanner.scan_state()
    last = ctx.db.scalar(select(ScanRun).order_by(ScanRun.started_at.desc()).limit(1))
    count = state.get("count") or (last.tracks_seen if last else 0)
    return ctx.ok(
        {
            "scanStatus": {
                "scanning": bool(state.get("scanning")),
                "count": count,
                "folderCount": 1,
                "lastScan": last.finished_at if last and last.finished_at else None,
            }
        }
    )


@endpoint(router, "startScan")
def start_scan(ctx: SubsonicContext = Depends(get_context)):
    _require_admin(ctx)
    if not scanner.is_scanning():
        import threading

        threading.Thread(
            target=scanner.scan_library, kwargs={"full": False}, daemon=True
        ).start()
    return get_scan_status(ctx)


# ─── Now playing ───────────────────────────────────────────────────────────


@endpoint(router, "getNowPlaying")
def get_now_playing(ctx: SubsonicContext = Depends(get_context)):
    entries = []
    for entry in now_playing.entries():
        track = ctx.db.get(Track, entry["track_id"])
        if track is None:
            continue
        payload = track_dict(track)
        payload.update(
            {
                "username": entry["user"],
                "minutesAgo": entry["minutes_ago"],
                "playerId": 0,
                "playerName": entry["client"] or "",
            }
        )
        entries.append(payload)
    return ctx.ok({"nowPlaying": {"entry": entries}})


# ─── Users ─────────────────────────────────────────────────────────────────


@endpoint(router, "getUser")
def get_user(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    username = params.get("username") or ctx.user.username

    if username != ctx.user.username and not ctx.user.is_admin:
        raise SubsonicError(
            SubsonicError.NOT_AUTHORIZED, "User is not authorized for the given operation"
        )

    target = ctx.db.scalar(select(User).where(User.username == username))
    if target is None:
        raise SubsonicError(SubsonicError.NOT_FOUND, f"User {username} not found")
    return ctx.ok({"user": user_dict(target)})


@endpoint(router, "getUsers")
def get_users(ctx: SubsonicContext = Depends(get_context)):
    _require_admin(ctx)
    users = ctx.db.scalars(select(User).order_by(User.username)).all()
    return ctx.ok({"users": {"user": [user_dict(u) for u in users]}})


@endpoint(router, "createUser")
def create_user(request: Request, ctx: SubsonicContext = Depends(get_context)):
    _require_admin(ctx)
    params = params_of(request)

    username = params.get("username")
    password = params.get("password")
    if not username or not password:
        raise SubsonicError(
            SubsonicError.MISSING_PARAMETER, "Both 'username' and 'password' are required"
        )
    if ctx.db.scalar(select(User).where(User.username == username)):
        raise SubsonicError(SubsonicError.GENERIC, f"User {username} already exists")

    from ..security import decode_subsonic_password

    user = create_local_user(
        ctx.db,
        username,
        decode_subsonic_password(password),
        email=params.get("email"),
        is_admin=param_bool(params, "adminRole"),
    )
    for flag, attribute in (
        ("downloadRole", "download_role"),
        ("uploadRole", "upload_role"),
        ("playlistRole", "playlist_role"),
        ("coverArtRole", "cover_art_role"),
        ("commentRole", "comment_role"),
        ("podcastRole", "podcast_role"),
        ("streamRole", "stream_role"),
        ("jukeboxRole", "jukebox_role"),
        ("shareRole", "share_role"),
    ):
        if flag in params:
            setattr(user, attribute, param_bool(params, flag))
    ctx.db.add(user)
    ctx.db.commit()

    seed_default_playlists(ctx.db, user)
    return ctx.ok()


@endpoint(router, "updateUser")
def update_user(request: Request, ctx: SubsonicContext = Depends(get_context)):
    _require_admin(ctx)
    params = params_of(request)

    username = params.get("username")
    user = ctx.db.scalar(select(User).where(User.username == username))
    if user is None:
        raise SubsonicError(SubsonicError.NOT_FOUND, f"User {username} not found")

    if params.get("password"):
        from ..security import decode_subsonic_password

        set_password(ctx.db, user, decode_subsonic_password(params["password"]))
    if "email" in params:
        user.email = params["email"]
    if "adminRole" in params:
        user.is_admin = param_bool(params, "adminRole")
    if "maxBitRate" in params:
        user.max_bitrate = param_int(params, "maxBitRate", 0) or 0
    for flag, attribute in (
        ("downloadRole", "download_role"),
        ("uploadRole", "upload_role"),
        ("playlistRole", "playlist_role"),
        ("coverArtRole", "cover_art_role"),
        ("commentRole", "comment_role"),
        ("podcastRole", "podcast_role"),
        ("streamRole", "stream_role"),
        ("jukeboxRole", "jukebox_role"),
        ("shareRole", "share_role"),
    ):
        if flag in params:
            setattr(user, attribute, param_bool(params, flag))

    ctx.db.add(user)
    ctx.db.commit()
    return ctx.ok()


@endpoint(router, "deleteUser")
def delete_user(request: Request, ctx: SubsonicContext = Depends(get_context)):
    _require_admin(ctx)
    params = params_of(request)

    username = params.get("username")
    user = ctx.db.scalar(select(User).where(User.username == username))
    if user is None:
        raise SubsonicError(SubsonicError.NOT_FOUND, f"User {username} not found")
    if user.id == ctx.user.id:
        raise SubsonicError(SubsonicError.GENERIC, "You cannot delete your own account")

    ctx.db.delete(user)
    ctx.db.commit()
    return ctx.ok()


@endpoint(router, "changePassword")
def change_password(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    username = params.get("username") or ctx.user.username
    password = params.get("password")

    if not password:
        raise SubsonicError(SubsonicError.MISSING_PARAMETER, "Parameter 'password' is missing")
    if username != ctx.user.username and not ctx.user.is_admin:
        raise SubsonicError(
            SubsonicError.NOT_AUTHORIZED, "User is not authorized for the given operation"
        )

    user = ctx.db.scalar(select(User).where(User.username == username))
    if user is None:
        raise SubsonicError(SubsonicError.NOT_FOUND, f"User {username} not found")

    from ..security import decode_subsonic_password

    set_password(ctx.db, user, decode_subsonic_password(password))
    return ctx.ok()


# ─── Play queue ────────────────────────────────────────────────────────────


@endpoint(router, "getPlayQueue")
def get_play_queue(ctx: SubsonicContext = Depends(get_context)):
    queue = ctx.db.scalar(select(PlayQueue).where(PlayQueue.user_id == ctx.user.id))
    if queue is None or not queue.track_ids:
        return ctx.ok()

    tracks = []
    for track_id in queue.track_ids:
        track = ctx.db.get(Track, track_id)
        if track is not None:
            tracks.append(track)

    return ctx.ok(
        {
            "playQueue": {
                "current": make_id(TRACK, queue.current_track_id)
                if queue.current_track_id
                else None,
                "position": queue.position_ms,
                "username": ctx.user.username,
                "changed": queue.changed_at,
                "changedBy": queue.changed_by or ctx.client,
                "entry": tracks_payload(ctx.db, ctx.user, tracks),
            }
        }
    )


@endpoint(router, "savePlayQueue")
def save_play_queue(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    raw_ids = request.query_params.getlist("id") or []
    if not raw_ids and params.get("id"):
        raw_ids = [params["id"]]

    track_ids = []
    for raw in raw_ids:
        try:
            track_ids.append(parse_typed_id(raw, TRACK))
        except SubsonicError:
            continue

    queue = ctx.db.scalar(select(PlayQueue).where(PlayQueue.user_id == ctx.user.id))
    if queue is None:
        queue = PlayQueue(user_id=ctx.user.id)
        ctx.db.add(queue)

    queue.track_ids = track_ids
    current = params.get("current")
    queue.current_track_id = (
        parse_typed_id(current, TRACK) if current else None
    )
    queue.position_ms = param_int(params, "position", 0) or 0
    queue.changed_by = ctx.client
    queue.changed_at = utcnow()
    ctx.db.commit()
    return ctx.ok()


# ─── Bookmarks ─────────────────────────────────────────────────────────────


@endpoint(router, "getBookmarks")
def get_bookmarks(ctx: SubsonicContext = Depends(get_context)):
    bookmarks = ctx.db.scalars(
        select(Bookmark).where(Bookmark.user_id == ctx.user.id)
    ).all()

    entries = []
    for bookmark in bookmarks:
        track = ctx.db.get(Track, bookmark.track_id)
        if track is None:
            continue
        entries.append(
            {
                "position": bookmark.position_ms,
                "username": ctx.user.username,
                "comment": bookmark.comment or None,
                "created": bookmark.created_at,
                "changed": bookmark.updated_at,
                "entry": track_dict(track),
            }
        )
    return ctx.ok({"bookmarks": {"bookmark": entries}})


@endpoint(router, "createBookmark")
def create_bookmark(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    track_id = parse_typed_id(params.get("id"), TRACK)
    position = param_int(params, "position", 0) or 0

    bookmark = ctx.db.scalar(
        select(Bookmark).where(
            Bookmark.user_id == ctx.user.id, Bookmark.track_id == track_id
        )
    )
    if bookmark is None:
        bookmark = Bookmark(user_id=ctx.user.id, track_id=track_id)
        ctx.db.add(bookmark)

    bookmark.position_ms = position
    bookmark.comment = params.get("comment", "") or ""
    bookmark.updated_at = utcnow()
    ctx.db.commit()
    return ctx.ok()


@endpoint(router, "deleteBookmark")
def delete_bookmark(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    track_id = parse_typed_id(params.get("id"), TRACK)

    bookmark = ctx.db.scalar(
        select(Bookmark).where(
            Bookmark.user_id == ctx.user.id, Bookmark.track_id == track_id
        )
    )
    if bookmark is not None:
        ctx.db.delete(bookmark)
        ctx.db.commit()
    return ctx.ok()


# ─── Internet radio ────────────────────────────────────────────────────────


@endpoint(router, "getInternetRadioStations")
def get_internet_radio_stations(ctx: SubsonicContext = Depends(get_context)):
    stations = ctx.db.scalars(
        select(InternetRadioStation).order_by(InternetRadioStation.name)
    ).all()
    return ctx.ok(
        {
            "internetRadioStations": {
                "internetRadioStation": [
                    {
                        "id": str(station.id),
                        "name": station.name,
                        "streamUrl": station.stream_url,
                        "homePageUrl": station.home_page_url or None,
                    }
                    for station in stations
                ]
            }
        }
    )


@endpoint(router, "createInternetRadioStation")
def create_internet_radio_station(
    request: Request, ctx: SubsonicContext = Depends(get_context)
):
    _require_admin(ctx)
    params = params_of(request)
    if not params.get("streamUrl") or not params.get("name"):
        raise SubsonicError(
            SubsonicError.MISSING_PARAMETER, "Both 'streamUrl' and 'name' are required"
        )

    ctx.db.add(
        InternetRadioStation(
            name=params["name"],
            stream_url=params["streamUrl"],
            home_page_url=params.get("homepageUrl", "") or "",
        )
    )
    ctx.db.commit()
    return ctx.ok()


@endpoint(router, "updateInternetRadioStation")
def update_internet_radio_station(
    request: Request, ctx: SubsonicContext = Depends(get_context)
):
    _require_admin(ctx)
    params = params_of(request)
    station = ctx.db.get(InternetRadioStation, param_int(params, "id", 0))
    if station is None:
        raise SubsonicError(SubsonicError.NOT_FOUND, "Radio station not found")

    if params.get("name"):
        station.name = params["name"]
    if params.get("streamUrl"):
        station.stream_url = params["streamUrl"]
    if "homepageUrl" in params:
        station.home_page_url = params["homepageUrl"]
    ctx.db.commit()
    return ctx.ok()


@endpoint(router, "deleteInternetRadioStation")
def delete_internet_radio_station(
    request: Request, ctx: SubsonicContext = Depends(get_context)
):
    _require_admin(ctx)
    params = params_of(request)
    station = ctx.db.get(InternetRadioStation, param_int(params, "id", 0))
    if station is not None:
        ctx.db.delete(station)
        ctx.db.commit()
    return ctx.ok()


# ─── Shares ────────────────────────────────────────────────────────────────


@endpoint(router, "getShares")
def get_shares(ctx: SubsonicContext = Depends(get_context)):
    shares = ctx.db.scalars(select(Share).where(Share.user_id == ctx.user.id)).all()
    entries = []
    for share in shares:
        tracks = [
            track
            for track in (ctx.db.get(Track, tid) for tid in share.item_ids)
            if track is not None
        ]
        entries.append(
            {
                "id": str(share.id),
                "url": f"{settings.base_url}/share/{share.token}",
                "description": share.description or None,
                "username": ctx.user.username,
                "created": share.created_at,
                "expires": share.expires_at,
                "lastVisited": share.last_visited_at,
                "visitCount": share.visit_count,
                "entry": tracks_payload(ctx.db, ctx.user, tracks),
            }
        )
    return ctx.ok({"shares": {"share": entries}})


@endpoint(router, "createShare")
def create_share(request: Request, ctx: SubsonicContext = Depends(get_context)):
    if not ctx.user.share_role:
        raise SubsonicError(
            SubsonicError.NOT_AUTHORIZED, "User is not authorized to create shares"
        )

    import secrets as _secrets

    params = params_of(request)
    raw_ids = request.query_params.getlist("id") or ([params["id"]] if params.get("id") else [])
    track_ids = []
    for raw in raw_ids:
        try:
            track_ids.append(parse_typed_id(raw, TRACK))
        except SubsonicError:
            continue

    share = Share(
        token=_secrets.token_urlsafe(12),
        user_id=ctx.user.id,
        description=params.get("description", "") or "",
        item_type="track",
        item_ids=track_ids,
    )
    ctx.db.add(share)
    ctx.db.commit()
    return get_shares(ctx)


@endpoint(router, "deleteShare")
def delete_share(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    share = ctx.db.get(Share, param_int(params, "id", 0))
    if share is None or (share.user_id != ctx.user.id and not ctx.user.is_admin):
        raise SubsonicError(SubsonicError.NOT_FOUND, "Share not found")
    ctx.db.delete(share)
    ctx.db.commit()
    return ctx.ok()


# ─── Unimplemented-by-design ───────────────────────────────────────────────


@endpoint(router, "jukeboxControl")
def jukebox_control(ctx: SubsonicContext = Depends(get_context)):
    # Musicdrome streams to clients; it does not drive server-attached audio.
    raise SubsonicError(
        SubsonicError.NOT_AUTHORIZED, "Jukebox mode is not supported by this server"
    )
