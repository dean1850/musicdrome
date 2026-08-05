"""Playlist endpoints: manual, rule-based (smart) and AI-curated."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..db import get_db, utcnow
from ..models import Playlist, PlaylistTrack, Track, User
from ..services.ai.curator import create_ai_playlist, generate_playlist, save_playlist
from ..services.ai.provider import AIError
from ..services.smartplaylist import (
    DEFAULT_PLAYLISTS,
    RuleError,
    refresh_playlist,
    validate_rules,
)
from .routes_library import track_out
from .schemas import (
    AIPlaylistRequest,
    GenericResponse,
    PlaylistCreateRequest,
    PlaylistDetail,
    PlaylistOut,
    PlaylistUpdateRequest,
    SmartPlaylistRequest,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["playlists"])


def _playlist_out(db: Session, playlist: Playlist) -> PlaylistOut:
    owner = db.get(User, playlist.owner_id)
    return PlaylistOut(
        id=playlist.id,
        name=playlist.name,
        comment=playlist.comment or "",
        owner_id=playlist.owner_id,
        owner=owner.username if owner else None,
        public=playlist.public,
        is_smart=playlist.is_smart,
        is_ai=playlist.is_ai,
        rules=playlist.rules,
        ai_prompt=playlist.ai_prompt,
        ai_rationale=playlist.ai_rationale,
        song_count=playlist.song_count,
        duration=playlist.duration,
        created_at=playlist.created_at,
        updated_at=playlist.updated_at,
        last_generated_at=playlist.last_generated_at,
    )


def _load(db: Session, playlist_id: int, user: User, *, for_write: bool = False) -> Playlist:
    playlist = db.get(Playlist, playlist_id)
    if playlist is None or (playlist.owner_id != user.id and not playlist.public):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playlist not found")
    if for_write and playlist.owner_id != user.id and not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only modify your own playlists",
        )
    return playlist


def _recalculate(db: Session, playlist: Playlist) -> None:
    entries = db.scalars(
        select(PlaylistTrack)
        .where(PlaylistTrack.playlist_id == playlist.id)
        .order_by(PlaylistTrack.position)
    ).all()
    playlist.song_count = len(entries)
    playlist.duration = sum(e.track.duration for e in entries if e.track is not None)
    playlist.cover_art_path = next(
        (e.track.cover_art_path for e in entries if e.track and e.track.cover_art_path),
        None,
    )
    playlist.updated_at = utcnow()
    db.add(playlist)


# ─── CRUD ──────────────────────────────────────────────────────────────────


@router.get("/playlists", response_model=list[PlaylistOut])
def list_playlists(
    kind: str = Query("all", pattern="^(all|manual|smart|ai)$"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(Playlist).where(
        or_(Playlist.owner_id == user.id, Playlist.public.is_(True))
    )
    if kind == "smart":
        stmt = stmt.where(Playlist.is_smart.is_(True))
    elif kind == "ai":
        stmt = stmt.where(Playlist.is_ai.is_(True))
    elif kind == "manual":
        stmt = stmt.where(Playlist.is_smart.is_(False), Playlist.is_ai.is_(False))

    playlists = db.scalars(stmt.order_by(Playlist.name)).all()
    return [_playlist_out(db, playlist) for playlist in playlists]


@router.get("/playlists/{playlist_id}", response_model=PlaylistDetail)
def get_playlist(
    playlist_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    playlist = _load(db, playlist_id, user)
    entries = db.scalars(
        select(PlaylistTrack)
        .where(PlaylistTrack.playlist_id == playlist.id)
        .order_by(PlaylistTrack.position)
    ).all()

    from .routes_library import _annotations
    from ..models import ItemType

    tracks = [entry for entry in entries if entry.track is not None]
    annotations = _annotations(
        db, user, ItemType.TRACK.value, [entry.track_id for entry in tracks]
    )

    detail = _playlist_out(db, playlist).model_dump()
    detail["tracks"] = [
        track_out(entry.track, annotations.get(entry.track_id), note=entry.note)
        for entry in tracks
    ]
    return PlaylistDetail(**detail)


@router.post("/playlists", response_model=PlaylistOut, status_code=status.HTTP_201_CREATED)
def create_playlist(
    payload: PlaylistCreateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user.playlist_role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Playlist management is disabled"
        )

    playlist = Playlist(
        name=payload.name,
        comment=payload.comment,
        owner_id=user.id,
        public=payload.public,
    )
    db.add(playlist)
    db.flush()

    for position, track_id in enumerate(payload.track_ids):
        if db.get(Track, track_id) is not None:
            db.add(
                PlaylistTrack(
                    playlist_id=playlist.id, track_id=track_id, position=position
                )
            )

    db.flush()
    _recalculate(db, playlist)
    db.commit()
    db.refresh(playlist)
    return _playlist_out(db, playlist)


@router.put("/playlists/{playlist_id}", response_model=PlaylistOut)
def update_playlist(
    playlist_id: int,
    payload: PlaylistUpdateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    playlist = _load(db, playlist_id, user, for_write=True)

    if payload.name is not None:
        playlist.name = payload.name
    if payload.comment is not None:
        playlist.comment = payload.comment
    if payload.public is not None:
        playlist.public = payload.public

    if payload.track_ids is not None:
        db.query(PlaylistTrack).filter(
            PlaylistTrack.playlist_id == playlist.id
        ).delete(synchronize_session=False)
        for position, track_id in enumerate(payload.track_ids):
            if db.get(Track, track_id) is not None:
                db.add(
                    PlaylistTrack(
                        playlist_id=playlist.id, track_id=track_id, position=position
                    )
                )
        # Hand-editing the track list detaches it from its generator
        playlist.is_smart = False
        playlist.is_ai = False
        playlist.rules = None

    db.flush()
    _recalculate(db, playlist)
    db.commit()
    db.refresh(playlist)
    return _playlist_out(db, playlist)


@router.delete("/playlists/{playlist_id}", response_model=GenericResponse)
def delete_playlist(
    playlist_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    playlist = _load(db, playlist_id, user, for_write=True)
    db.delete(playlist)
    db.commit()
    return GenericResponse(message="Playlist deleted")


@router.post("/playlists/{playlist_id}/tracks", response_model=PlaylistOut)
def add_tracks(
    playlist_id: int,
    track_ids: list[int],
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    playlist = _load(db, playlist_id, user, for_write=True)

    position = db.scalar(
        select(PlaylistTrack.position)
        .where(PlaylistTrack.playlist_id == playlist.id)
        .order_by(PlaylistTrack.position.desc())
        .limit(1)
    )
    next_position = (position or -1) + 1

    for track_id in track_ids:
        if db.get(Track, track_id) is None:
            continue
        db.add(
            PlaylistTrack(
                playlist_id=playlist.id, track_id=track_id, position=next_position
            )
        )
        next_position += 1

    db.flush()
    _recalculate(db, playlist)
    db.commit()
    db.refresh(playlist)
    return _playlist_out(db, playlist)


# ─── Smart playlists ───────────────────────────────────────────────────────


@router.get("/playlists-smart/templates")
def smart_templates(user: User = Depends(get_current_user)):
    """The starter rule sets, offered as a starting point in the UI."""
    return DEFAULT_PLAYLISTS


@router.post("/playlists-smart", response_model=PlaylistOut, status_code=status.HTTP_201_CREATED)
def create_smart_playlist(
    payload: SmartPlaylistRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        validate_rules(payload.rules)
    except RuleError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    playlist = Playlist(
        name=payload.name,
        comment=payload.comment,
        owner_id=user.id,
        public=payload.public,
        is_smart=True,
        rules=payload.rules,
    )
    db.add(playlist)
    db.flush()

    refresh_playlist(db, playlist)
    db.commit()
    db.refresh(playlist)
    return _playlist_out(db, playlist)


@router.put("/playlists-smart/{playlist_id}", response_model=PlaylistOut)
def update_smart_playlist(
    playlist_id: int,
    payload: SmartPlaylistRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    playlist = _load(db, playlist_id, user, for_write=True)
    try:
        validate_rules(payload.rules)
    except RuleError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    playlist.name = payload.name
    playlist.comment = payload.comment
    playlist.public = payload.public
    playlist.is_smart = True
    playlist.is_ai = False
    playlist.rules = payload.rules

    refresh_playlist(db, playlist)
    db.commit()
    db.refresh(playlist)
    return _playlist_out(db, playlist)


@router.post("/playlists/{playlist_id}/refresh", response_model=PlaylistOut)
def refresh_generated_playlist(
    playlist_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Re-run a smart playlist's rules, or re-curate an AI playlist."""
    playlist = _load(db, playlist_id, user, for_write=True)

    if playlist.is_smart:
        refresh_playlist(db, playlist)
        db.commit()
    elif playlist.is_ai:
        brief = playlist.ai_prompt or playlist.name
        try:
            selection = generate_playlist(db, user, brief)
        except AIError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            )
        save_playlist(db, user, selection, brief=brief, playlist=playlist)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only smart and AI playlists can be refreshed",
        )

    db.refresh(playlist)
    return _playlist_out(db, playlist)


# ─── AI playlists ──────────────────────────────────────────────────────────


@router.post("/playlists-ai", response_model=PlaylistOut, status_code=status.HTTP_201_CREATED)
def create_ai_curated_playlist(
    payload: AIPlaylistRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user.ai_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="AI features are turned off for your account",
        )
    try:
        playlist = create_ai_playlist(
            db,
            user,
            payload.brief,
            max_tracks=payload.max_tracks,
            seed_genre=payload.seed_genre,
        )
    except AIError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    return _playlist_out(db, playlist)


@router.post("/playlists-ai/preview")
def preview_ai_playlist(
    payload: AIPlaylistRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Curate without saving, so the user can review before committing."""
    if not user.ai_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="AI features are turned off for your account",
        )
    try:
        selection = generate_playlist(
            db,
            user,
            payload.brief,
            max_tracks=payload.max_tracks,
            seed_genre=payload.seed_genre,
        )
    except AIError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))

    tracks = []
    for entry in selection["tracks"]:
        track = db.get(Track, entry["id"])
        if track is not None:
            tracks.append(
                {**track_out(track).model_dump(), "note": entry.get("reason", "")}
            )

    return {
        "name": selection["name"],
        "description": selection["description"],
        "rationale": selection["rationale"],
        "model": selection.get("model", ""),
        "tracks": tracks,
    }
