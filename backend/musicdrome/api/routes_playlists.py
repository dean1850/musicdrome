"""Playlist endpoints: manual, rule-based (smart) and AI-curated."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import PlainTextResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..auth import get_current_admin, get_current_user
from ..config import settings
from ..db import get_db, utcnow
from ..models import Playlist, PlaylistTrack, Track, User
from ..services import playlistfile
from ..services.ai.curator import create_ai_playlist, generate_playlist, save_playlist
from ..services.ai.provider import AIError
from ..services.playlists import detach, recalculate, replace_tracks
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
    PlaylistImportRequest,
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
        is_imported=playlist.is_imported,
        import_path=playlist.import_path,
        import_missing=playlist.import_missing,
        sync=playlist.sync,
        imported_at=playlist.imported_at,
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


# ─── CRUD ──────────────────────────────────────────────────────────────────


@router.get("/playlists", response_model=list[PlaylistOut])
def list_playlists(
    kind: str = Query("all", pattern="^(all|manual|smart|ai|imported)$"),
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
    elif kind == "imported":
        stmt = stmt.where(Playlist.is_imported.is_(True))
    elif kind == "manual":
        # Anything a person can edit freely: no rules, no model, no file.
        stmt = stmt.where(
            Playlist.is_smart.is_(False),
            Playlist.is_ai.is_(False),
            Playlist.sync.is_(False),
        )

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

    replace_tracks(db, playlist, payload.track_ids)
    recalculate(db, playlist)
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
        replace_tracks(db, playlist, payload.track_ids)
        # Hand-editing the track list detaches it from whatever was generating
        # it — rules, a model, or an .m3u on disk.
        detach(playlist)

    db.flush()
    recalculate(db, playlist)
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

    added = False
    for track_id in track_ids:
        if db.get(Track, track_id) is None:
            continue
        db.add(
            PlaylistTrack(
                playlist_id=playlist.id, track_id=track_id, position=next_position
            )
        )
        next_position += 1
        added = True

    if added:
        detach(playlist)

    db.flush()
    recalculate(db, playlist)
    db.commit()
    db.refresh(playlist)
    return _playlist_out(db, playlist)


# ─── M3U import / export ───────────────────────────────────────────────────
#
# Paths deliberately mirror /playlists-smart and /playlists-ai: a literal
# segment under /playlists/ would be swallowed by /playlists/{playlist_id}.


@router.post("/playlists-import", response_model=GenericResponse)
def import_playlist_files(
    payload: PlaylistImportRequest,
    admin: User = Depends(get_current_admin),
):
    """Re-read every ``.m3u`` under the configured roots, now.

    Normally unnecessary — the watcher picks a file up seconds after it is
    written, and the scanner sweeps on its own schedule — but useful right
    after pointing the server at a folder full of them.
    """
    stats = playlistfile.import_all(force=payload.force)
    if stats.get("skipped"):
        return GenericResponse(ok=False, message="An import is already running")

    return GenericResponse(
        message=(
            f"{stats['files']} playlist file(s): {stats['created']} imported, "
            f"{stats['updated']} updated, {stats['deleted']} removed"
        ),
        data={key: value for key, value in stats.items() if key != "at"},
    )


@router.get("/playlists-import/status")
def playlist_import_status(admin: User = Depends(get_current_admin)):
    # Counting walks each root, which is why this is not on the playlists page:
    # it answers "why has it not found my file" and is asked once, by an admin.
    roots = [
        {
            "path": str(root),
            "exists": root.is_dir(),
            "files": len(playlistfile.discover([root])) if root.is_dir() else 0,
        }
        for root in settings.playlist_import_roots
    ]
    return {
        "enabled": settings.playlist_auto_import,
        "roots": roots,
        "extensions": sorted(settings.playlist_extensions),
        "interval_minutes": settings.playlist_import_interval_minutes,
        "public": settings.playlist_import_public,
        "prune": settings.playlist_import_prune,
        "last_run": playlistfile.last_run(),
    }


@router.post(
    "/playlists-import/upload",
    response_model=PlaylistOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_playlist_file(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Import a single ``.m3u`` the user hands over, rather than one on disk.

    The result belongs to the uploader and is not bound to a file, so it
    behaves like any hand-built playlist from here on.
    """
    if not user.playlist_role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Playlist management is disabled"
        )

    raw = await file.read()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="The file is empty"
        )

    name = Path(file.filename or "Imported playlist").stem
    document = playlistfile.parse_m3u_bytes(raw, name=name)
    if not document.entries:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No playlist entries found — is this an M3U file?",
        )

    # Relative entries have no folder to resolve against here, so they fall to
    # the path-tail and metadata steps of the ladder.
    index = playlistfile.build_index(db)
    track_ids, missing = playlistfile.resolve_document(
        document, settings.music_dir, index
    )
    if not track_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"None of the {len(missing)} entries match a track in your library"
            ),
        )

    playlist = Playlist(
        name=document.name or name,
        comment=f"Imported from {file.filename}",
        owner_id=user.id,
        public=False,
        is_imported=True,
        sync=False,
        import_missing=len(missing),
        imported_at=utcnow(),
    )
    db.add(playlist)
    db.flush()

    replace_tracks(db, playlist, track_ids)
    recalculate(db, playlist)
    db.commit()
    db.refresh(playlist)
    return _playlist_out(db, playlist)


@router.get("/playlists/{playlist_id}/export.m3u", response_class=PlainTextResponse)
def export_playlist(
    playlist_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    playlist = _load(db, playlist_id, user)
    safe_name = "".join(
        char if char.isalnum() or char in " -_" else "_" for char in playlist.name
    ).strip() or "playlist"

    return PlainTextResponse(
        playlistfile.export_m3u(db, playlist),
        media_type="audio/x-mpegurl",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.m3u"'},
    )


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
