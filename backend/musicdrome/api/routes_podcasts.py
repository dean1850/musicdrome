"""Podcast endpoints for the web UI."""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..config import settings
from ..db import get_db, session_scope
from ..models import PodcastChannel, PodcastEpisode, User
from ..services import podcasts as podcast_service
from .schemas import (
    GenericResponse,
    PodcastChannelOut,
    PodcastEpisodeOut,
    PodcastSubscribeRequest,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["podcasts"])


def _channel_out(db: Session, channel: PodcastChannel) -> PodcastChannelOut:
    count = db.scalar(
        select(func.count(PodcastEpisode.id)).where(
            PodcastEpisode.channel_id == channel.id,
            PodcastEpisode.status != "deleted",
        )
    ) or 0
    return PodcastChannelOut(
        id=channel.id,
        url=channel.url,
        title=channel.title or channel.url,
        description=channel.description or "",
        author=channel.author or "",
        image_url=channel.image_url,
        status=channel.status,
        error_message=channel.error_message,
        auto_download=channel.auto_download,
        episode_count=count,
        last_fetched_at=channel.last_fetched_at,
    )


@router.get("/podcasts", response_model=list[PodcastChannelOut])
def list_channels(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    channels = db.scalars(select(PodcastChannel).order_by(PodcastChannel.title)).all()
    return [_channel_out(db, channel) for channel in channels]


@router.post("/podcasts", response_model=PodcastChannelOut, status_code=status.HTTP_201_CREATED)
def subscribe(
    payload: PodcastSubscribeRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user.podcast_role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Podcast management is disabled"
        )
    if not settings.podcast_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Podcasts are disabled (PODCAST_ENABLED=false)",
        )

    try:
        channel = podcast_service.add_channel(db, payload.url, user)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return _channel_out(db, channel)


@router.get("/podcasts/{channel_id}")
def get_channel(
    channel_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    channel = db.get(PodcastChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")

    episodes = db.scalars(
        select(PodcastEpisode)
        .where(
            PodcastEpisode.channel_id == channel.id,
            PodcastEpisode.status != "deleted",
        )
        .order_by(PodcastEpisode.publish_date.desc().nullslast())
    ).all()

    return {
        "channel": _channel_out(db, channel),
        "episodes": [
            PodcastEpisodeOut.model_validate(episode, from_attributes=True)
            for episode in episodes
        ],
    }


@router.delete("/podcasts/{channel_id}", response_model=GenericResponse)
def unsubscribe(
    channel_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user.podcast_role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Podcast management is disabled"
        )
    channel = db.get(PodcastChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")

    podcast_service.delete_channel(db, channel)
    return GenericResponse(message="Unsubscribed")


@router.put("/podcasts/{channel_id}", response_model=PodcastChannelOut)
def update_channel(
    channel_id: int,
    auto_download: bool = Body(..., embed=True),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    channel = db.get(PodcastChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")

    channel.auto_download = auto_download
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return _channel_out(db, channel)


@router.post("/podcasts/{channel_id}/refresh", response_model=PodcastChannelOut)
def refresh_channel(
    channel_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    channel = db.get(PodcastChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")

    try:
        podcast_service.refresh_channel(db, channel)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    db.refresh(channel)
    return _channel_out(db, channel)


@router.post("/podcasts/refresh-all", response_model=GenericResponse)
def refresh_all(user: User = Depends(get_current_user)):
    threading.Thread(target=podcast_service.refresh_all, daemon=True).start()
    return GenericResponse(message="Refreshing all podcast feeds in the background")


# ─── Episodes ──────────────────────────────────────────────────────────────


@router.post("/podcasts/episodes/{episode_id}/download", response_model=GenericResponse)
def download_episode(
    episode_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user.podcast_role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Podcast management is disabled"
        )
    if db.get(PodcastEpisode, episode_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Episode not found")

    def worker() -> None:
        with session_scope() as scoped:
            episode = scoped.get(PodcastEpisode, episode_id)
            if episode is not None:
                try:
                    podcast_service.download_episode(scoped, episode)
                except Exception:
                    log.exception("podcast download failed")

    threading.Thread(target=worker, daemon=True).start()
    return GenericResponse(message="Download started")


@router.delete("/podcasts/episodes/{episode_id}", response_model=GenericResponse)
def delete_episode(
    episode_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    episode = db.get(PodcastEpisode, episode_id)
    if episode is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Episode not found")

    podcast_service.delete_episode(db, episode)
    return GenericResponse(message="Episode file removed")


@router.get("/podcasts/episodes/{episode_id}/stream")
def stream_episode(
    episode_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from pathlib import Path

    episode = db.get(PodcastEpisode, episode_id)
    if episode is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Episode not found")
    if not episode.path or not Path(episode.path).exists():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Episode has not been downloaded yet",
        )

    return FileResponse(
        episode.path,
        media_type=episode.content_type or "audio/mpeg",
        headers={"Accept-Ranges": "bytes"},
    )


# ─── OPML ──────────────────────────────────────────────────────────────────


@router.get("/podcasts-opml")
def export_opml(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return Response(
        content=podcast_service.export_opml(db),
        media_type="application/xml",
        headers={"Content-Disposition": 'attachment; filename="musicdrome-podcasts.opml"'},
    )


@router.post("/podcasts-opml", response_model=GenericResponse)
def import_opml(
    content: str = Body(..., embed=True),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user.podcast_role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Podcast management is disabled"
        )
    try:
        channels = podcast_service.import_opml(db, content, user)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return GenericResponse(message=f"Subscribed to {len(channels)} feeds")
