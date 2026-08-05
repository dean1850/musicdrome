"""Subsonic playlist and podcast endpoints."""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Depends, Request
from sqlalchemy import or_, select

from ..db import utcnow
from ..models import (
    Playlist,
    PlaylistTrack,
    PodcastChannel,
    PodcastEpisode,
    Track,
    User,
)
from ..services import podcasts as podcast_service
from .common import (
    EPISODE,
    PLAYLIST,
    PODCAST,
    TRACK,
    SubsonicContext,
    SubsonicError,
    endpoint,
    get_context,
    make_id,
    param_bool,
    param_int,
    params_of,
    parse_typed_id,
    playlist_dict,
    tracks_payload,
)

log = logging.getLogger(__name__)

router = APIRouter()


# ─── Playlists ─────────────────────────────────────────────────────────────


def _visible_playlists(ctx: SubsonicContext):
    return select(Playlist).where(
        or_(Playlist.owner_id == ctx.user.id, Playlist.public.is_(True))
    )


def _load_playlist(ctx: SubsonicContext, raw_id: str | None, *, for_write: bool = False) -> Playlist:
    playlist_id = parse_typed_id(raw_id, PLAYLIST)
    playlist = ctx.db.get(Playlist, playlist_id)

    if playlist is None:
        raise SubsonicError(SubsonicError.NOT_FOUND, "Playlist not found")
    if playlist.owner_id != ctx.user.id and not playlist.public:
        raise SubsonicError(SubsonicError.NOT_FOUND, "Playlist not found")
    if for_write and playlist.owner_id != ctx.user.id and not ctx.user.is_admin:
        raise SubsonicError(
            SubsonicError.NOT_AUTHORIZED, "You can only modify your own playlists"
        )
    return playlist


@endpoint(router, "getPlaylists")
def get_playlists(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    stmt = _visible_playlists(ctx)

    # Admins may list another user's playlists
    username = params.get("username")
    if username and ctx.user.is_admin:
        other = ctx.db.scalar(select(User).where(User.username == username))
        if other is None:
            raise SubsonicError(SubsonicError.NOT_FOUND, f"User {username} not found")
        stmt = select(Playlist).where(Playlist.owner_id == other.id)

    playlists = ctx.db.scalars(stmt.order_by(Playlist.name)).all()
    owners = {
        user.id: user
        for user in ctx.db.scalars(
            select(User).where(User.id.in_([p.owner_id for p in playlists] or [0]))
        ).all()
    }
    return ctx.ok(
        {
            "playlists": {
                "playlist": [
                    playlist_dict(playlist, owners.get(playlist.owner_id))
                    for playlist in playlists
                ]
            }
        }
    )


@endpoint(router, "getPlaylist")
def get_playlist(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    playlist = _load_playlist(ctx, params.get("id"))

    tracks = [
        entry.track
        for entry in ctx.db.scalars(
            select(PlaylistTrack)
            .where(PlaylistTrack.playlist_id == playlist.id)
            .order_by(PlaylistTrack.position)
        ).all()
        if entry.track is not None
    ]

    owner = ctx.db.get(User, playlist.owner_id)
    payload = playlist_dict(playlist, owner)
    payload["entry"] = tracks_payload(ctx.db, ctx.user, tracks)
    return ctx.ok({"playlist": payload})


def _collect_ids(request: Request, params: dict, name: str) -> list[str]:
    values = request.query_params.getlist(name)
    if not values and params.get(name):
        values = [params[name]]
    return values


def _recalculate(ctx: SubsonicContext, playlist: Playlist) -> None:
    entries = ctx.db.scalars(
        select(PlaylistTrack)
        .where(PlaylistTrack.playlist_id == playlist.id)
        .order_by(PlaylistTrack.position)
    ).all()

    playlist.song_count = len(entries)
    playlist.duration = sum(
        entry.track.duration for entry in entries if entry.track is not None
    )
    playlist.cover_art_path = next(
        (
            entry.track.cover_art_path
            for entry in entries
            if entry.track is not None and entry.track.cover_art_path
        ),
        None,
    )
    playlist.updated_at = utcnow()
    ctx.db.add(playlist)


@endpoint(router, "createPlaylist")
def create_playlist(request: Request, ctx: SubsonicContext = Depends(get_context)):
    if not ctx.user.playlist_role:
        raise SubsonicError(
            SubsonicError.NOT_AUTHORIZED, "User is not authorized to manage playlists"
        )

    params = params_of(request)
    raw_playlist_id = params.get("playlistId")

    if raw_playlist_id:
        playlist = _load_playlist(ctx, raw_playlist_id, for_write=True)
        ctx.db.query(PlaylistTrack).filter(
            PlaylistTrack.playlist_id == playlist.id
        ).delete(synchronize_session=False)
        if params.get("name"):
            playlist.name = params["name"]
    else:
        name = params.get("name")
        if not name:
            raise SubsonicError(
                SubsonicError.MISSING_PARAMETER, "Parameter 'name' is missing"
            )
        playlist = Playlist(name=name, owner_id=ctx.user.id, public=False)
        ctx.db.add(playlist)
        ctx.db.flush()

    # A smart or AI playlist becomes a plain one once edited by hand
    playlist.is_smart = False
    playlist.is_ai = False
    playlist.rules = None

    position = 0
    for raw in _collect_ids(request, params, "songId"):
        try:
            track_id = parse_typed_id(raw, TRACK)
        except SubsonicError:
            continue
        if ctx.db.get(Track, track_id) is None:
            continue
        ctx.db.add(
            PlaylistTrack(playlist_id=playlist.id, track_id=track_id, position=position)
        )
        position += 1

    _recalculate(ctx, playlist)
    ctx.db.commit()
    ctx.db.refresh(playlist)

    return get_playlist_by_object(ctx, playlist)


def get_playlist_by_object(ctx: SubsonicContext, playlist: Playlist):
    tracks = [
        entry.track
        for entry in ctx.db.scalars(
            select(PlaylistTrack)
            .where(PlaylistTrack.playlist_id == playlist.id)
            .order_by(PlaylistTrack.position)
        ).all()
        if entry.track is not None
    ]
    payload = playlist_dict(playlist, ctx.db.get(User, playlist.owner_id))
    payload["entry"] = tracks_payload(ctx.db, ctx.user, tracks)
    return ctx.ok({"playlist": payload})


@endpoint(router, "updatePlaylist")
def update_playlist(request: Request, ctx: SubsonicContext = Depends(get_context)):
    if not ctx.user.playlist_role:
        raise SubsonicError(
            SubsonicError.NOT_AUTHORIZED, "User is not authorized to manage playlists"
        )

    params = params_of(request)
    playlist = _load_playlist(ctx, params.get("playlistId"), for_write=True)

    if params.get("name"):
        playlist.name = params["name"]
    if "comment" in params:
        playlist.comment = params["comment"]
    if "public" in params:
        playlist.public = param_bool(params, "public")

    # Removals are indices into the current ordering — resolve them before
    # appending, so the caller's indices still mean what they meant.
    remove_indices = set()
    for raw in _collect_ids(request, params, "songIndexToRemove"):
        try:
            remove_indices.add(int(raw))
        except (TypeError, ValueError):
            continue

    entries = list(
        ctx.db.scalars(
            select(PlaylistTrack)
            .where(PlaylistTrack.playlist_id == playlist.id)
            .order_by(PlaylistTrack.position)
        ).all()
    )
    if remove_indices:
        for index, entry in enumerate(entries):
            if index in remove_indices:
                ctx.db.delete(entry)
        entries = [e for i, e in enumerate(entries) if i not in remove_indices]
        for position, entry in enumerate(entries):
            entry.position = position
            ctx.db.add(entry)

    next_position = len(entries)
    for raw in _collect_ids(request, params, "songIdToAdd"):
        try:
            track_id = parse_typed_id(raw, TRACK)
        except SubsonicError:
            continue
        if ctx.db.get(Track, track_id) is None:
            continue
        ctx.db.add(
            PlaylistTrack(
                playlist_id=playlist.id, track_id=track_id, position=next_position
            )
        )
        next_position += 1

    if remove_indices or _collect_ids(request, params, "songIdToAdd"):
        playlist.is_smart = False
        playlist.is_ai = False
        playlist.rules = None

    ctx.db.flush()
    _recalculate(ctx, playlist)
    ctx.db.commit()
    return ctx.ok()


@endpoint(router, "deletePlaylist")
def delete_playlist(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    playlist = _load_playlist(ctx, params.get("id"), for_write=True)
    ctx.db.delete(playlist)
    ctx.db.commit()
    return ctx.ok()


# ─── Podcasts ──────────────────────────────────────────────────────────────


def _channel_dict(channel: PodcastChannel) -> dict:
    return {
        "id": make_id(PODCAST, channel.id),
        "url": channel.url,
        "title": channel.title or channel.url,
        "description": channel.description or None,
        "coverArt": make_id(PODCAST, channel.id) if channel.image_path else None,
        "originalImageUrl": channel.image_url or None,
        "status": channel.status,
        "errorMessage": channel.error_message or None,
    }


def _episode_dict(episode: PodcastEpisode, channel: PodcastChannel) -> dict:
    return {
        "id": make_id(EPISODE, episode.id),
        "channelId": make_id(PODCAST, channel.id),
        "streamId": make_id(EPISODE, episode.id) if episode.path else None,
        "title": episode.title,
        "description": episode.description or None,
        "publishDate": episode.publish_date,
        "status": episode.status,
        "parent": make_id(PODCAST, channel.id),
        "isDir": False,
        "year": episode.publish_date.year if episode.publish_date else None,
        "coverArt": make_id(PODCAST, channel.id) if channel.image_path else None,
        "size": episode.size or None,
        "contentType": episode.content_type,
        "suffix": episode.suffix,
        "duration": episode.duration or None,
        "bitRate": episode.bitrate or None,
        "type": "podcast",
        "errorMessage": episode.error_message or None,
    }


@endpoint(router, "getPodcasts")
def get_podcasts(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    include_episodes = param_bool(params, "includeEpisodes", True)

    stmt = select(PodcastChannel).order_by(PodcastChannel.title)
    if params.get("id"):
        stmt = stmt.where(PodcastChannel.id == parse_typed_id(params["id"], PODCAST))

    channels = ctx.db.scalars(stmt).all()

    payload = []
    for channel in channels:
        entry = _channel_dict(channel)
        if include_episodes:
            episodes = ctx.db.scalars(
                select(PodcastEpisode)
                .where(
                    PodcastEpisode.channel_id == channel.id,
                    PodcastEpisode.status != "deleted",
                )
                .order_by(PodcastEpisode.publish_date.desc().nullslast())
            ).all()
            entry["episode"] = [_episode_dict(e, channel) for e in episodes]
        payload.append(entry)

    return ctx.ok({"podcasts": {"channel": payload}})


@endpoint(router, "getNewestPodcasts")
def get_newest_podcasts(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    count = param_int(params, "count", 20) or 20

    episodes = ctx.db.scalars(
        select(PodcastEpisode)
        .where(PodcastEpisode.status != "deleted")
        .order_by(PodcastEpisode.publish_date.desc().nullslast())
        .limit(count)
    ).all()

    entries = []
    for episode in episodes:
        channel = ctx.db.get(PodcastChannel, episode.channel_id)
        if channel is not None:
            entries.append(_episode_dict(episode, channel))
    return ctx.ok({"newestPodcasts": {"episode": entries}})


@endpoint(router, "refreshPodcasts")
def refresh_podcasts(ctx: SubsonicContext = Depends(get_context)):
    if not ctx.user.podcast_role:
        raise SubsonicError(
            SubsonicError.NOT_AUTHORIZED, "User is not authorized to manage podcasts"
        )
    threading.Thread(target=podcast_service.refresh_all, daemon=True).start()
    return ctx.ok()


@endpoint(router, "createPodcastChannel")
def create_podcast_channel(request: Request, ctx: SubsonicContext = Depends(get_context)):
    if not ctx.user.podcast_role:
        raise SubsonicError(
            SubsonicError.NOT_AUTHORIZED, "User is not authorized to manage podcasts"
        )

    params = params_of(request)
    url = params.get("url")
    if not url:
        raise SubsonicError(SubsonicError.MISSING_PARAMETER, "Parameter 'url' is missing")

    try:
        podcast_service.add_channel(ctx.db, url, ctx.user)
    except Exception as exc:
        raise SubsonicError(SubsonicError.GENERIC, f"Could not subscribe: {exc}")
    return ctx.ok()


@endpoint(router, "deletePodcastChannel")
def delete_podcast_channel(request: Request, ctx: SubsonicContext = Depends(get_context)):
    if not ctx.user.podcast_role:
        raise SubsonicError(
            SubsonicError.NOT_AUTHORIZED, "User is not authorized to manage podcasts"
        )

    params = params_of(request)
    channel = ctx.db.get(PodcastChannel, parse_typed_id(params.get("id"), PODCAST))
    if channel is None:
        raise SubsonicError(SubsonicError.NOT_FOUND, "Podcast channel not found")

    podcast_service.delete_channel(ctx.db, channel)
    return ctx.ok()


@endpoint(router, "downloadPodcastEpisode")
def download_podcast_episode(request: Request, ctx: SubsonicContext = Depends(get_context)):
    if not ctx.user.podcast_role:
        raise SubsonicError(
            SubsonicError.NOT_AUTHORIZED, "User is not authorized to manage podcasts"
        )

    params = params_of(request)
    episode_id = parse_typed_id(params.get("id"), EPISODE)
    episode = ctx.db.get(PodcastEpisode, episode_id)
    if episode is None:
        raise SubsonicError(SubsonicError.NOT_FOUND, "Episode not found")

    def worker() -> None:
        from ..db import session_scope

        with session_scope() as db:
            target = db.get(PodcastEpisode, episode_id)
            if target is not None:
                try:
                    podcast_service.download_episode(db, target)
                except Exception:
                    log.exception("podcast download failed")

    threading.Thread(target=worker, daemon=True).start()
    return ctx.ok()


@endpoint(router, "deletePodcastEpisode")
def delete_podcast_episode(request: Request, ctx: SubsonicContext = Depends(get_context)):
    if not ctx.user.podcast_role:
        raise SubsonicError(
            SubsonicError.NOT_AUTHORIZED, "User is not authorized to manage podcasts"
        )

    params = params_of(request)
    episode = ctx.db.get(PodcastEpisode, parse_typed_id(params.get("id"), EPISODE))
    if episode is None:
        raise SubsonicError(SubsonicError.NOT_FOUND, "Episode not found")

    podcast_service.delete_episode(ctx.db, episode)
    return ctx.ok()
