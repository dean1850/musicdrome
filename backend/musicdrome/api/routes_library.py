"""Library browsing, search, playback and annotation for the web UI."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..db import get_db, utcnow
from ..models import (
    Album,
    Annotation,
    Artist,
    ItemType,
    PlayHistory,
    SimilarArtist,
    Track,
    User,
)
from ..services import scrobble as scrobble_service
from ..services import transcode
from ..subsonic.common import now_playing
from ..subsonic.routes_media import _cover_path, _placeholder_cover
from .schemas import AlbumOut, ArtistOut, GenericResponse, SearchResults, TrackOut

log = logging.getLogger(__name__)

router = APIRouter(tags=["library"])


# ─── Serialisation ─────────────────────────────────────────────────────────


def _annotations(db: Session, user: User, item_type: str, ids: list[int]) -> dict[int, Annotation]:
    if not ids:
        return {}
    rows = db.scalars(
        select(Annotation).where(
            Annotation.user_id == user.id,
            Annotation.item_type == item_type,
            Annotation.item_id.in_(ids),
        )
    ).all()
    return {row.item_id: row for row in rows}


def artist_out(artist: Artist, annotation: Annotation | None = None) -> ArtistOut:
    return ArtistOut(
        id=artist.id,
        name=artist.name,
        album_count=artist.album_count,
        track_count=artist.track_count,
        mbid=artist.mbid,
        biography=artist.biography,
        image_url=artist.image_url,
        has_image=bool(artist.image_path),
        starred=bool(annotation and annotation.starred_at),
        rating=annotation.rating if annotation else 0,
    )


def album_out(album: Album, annotation: Annotation | None = None) -> AlbumOut:
    return AlbumOut(
        id=album.id,
        name=album.name,
        artist_id=album.artist_id,
        artist_name=album.artist_name,
        album_artist=album.album_artist,
        year=album.year,
        genre=album.genre,
        song_count=album.song_count,
        duration=album.duration,
        created_at=album.created_at,
        starred=bool(annotation and annotation.starred_at),
        rating=annotation.rating if annotation else 0,
        play_count=annotation.play_count if annotation else 0,
    )


def track_out(track: Track, annotation: Annotation | None = None, note: str | None = None) -> TrackOut:
    return TrackOut(
        id=track.id,
        title=track.title,
        album_id=track.album_id,
        album_name=track.album_name,
        artist_id=track.artist_id,
        artist_name=track.artist_name,
        track_number=track.track_number,
        disc_number=track.disc_number,
        year=track.year,
        genre=track.genre,
        duration=track.duration,
        bitrate=track.bitrate,
        suffix=track.suffix,
        content_type=track.content_type,
        size=track.size,
        starred=bool(annotation and annotation.starred_at),
        rating=annotation.rating if annotation else 0,
        play_count=annotation.play_count if annotation else 0,
        note=note,
    )


def tracks_out(db: Session, user: User, tracks: list[Track]) -> list[TrackOut]:
    annotations = _annotations(db, user, ItemType.TRACK.value, [t.id for t in tracks])
    return [track_out(track, annotations.get(track.id)) for track in tracks]


def albums_out(db: Session, user: User, albums: list[Album]) -> list[AlbumOut]:
    annotations = _annotations(db, user, ItemType.ALBUM.value, [a.id for a in albums])
    return [album_out(album, annotations.get(album.id)) for album in albums]


def artists_out(db: Session, user: User, artists: list[Artist]) -> list[ArtistOut]:
    annotations = _annotations(db, user, ItemType.ARTIST.value, [a.id for a in artists])
    return [artist_out(artist, annotations.get(artist.id)) for artist in artists]


# ─── Artists ───────────────────────────────────────────────────────────────


@router.get("/artists", response_model=list[ArtistOut])
def list_artists(
    q: str = Query("", description="Filter by name"),
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    starred: bool = Query(False),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(Artist)
    if q:
        stmt = stmt.where(Artist.name.ilike(f"%{q}%"))
    if starred:
        stmt = stmt.join(
            Annotation,
            and_(
                Annotation.item_id == Artist.id,
                Annotation.item_type == ItemType.ARTIST.value,
                Annotation.user_id == user.id,
            ),
        ).where(Annotation.starred_at.isnot(None))

    artists = list(
        db.scalars(
            stmt.order_by(Artist.sort_name, Artist.name).offset(offset).limit(limit)
        ).all()
    )
    return artists_out(db, user, artists)


@router.get("/artists/{artist_id}")
def get_artist(
    artist_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    artist = db.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artist not found")

    albums = list(
        db.scalars(
            select(Album)
            .where(Album.artist_id == artist.id)
            .order_by(Album.year.desc().nullslast(), Album.name)
        ).all()
    )
    similar = db.scalars(
        select(SimilarArtist)
        .where(SimilarArtist.artist_id == artist.id)
        .order_by(SimilarArtist.score.desc())
        .limit(20)
    ).all()

    annotation = db.scalar(
        select(Annotation).where(
            Annotation.user_id == user.id,
            Annotation.item_type == ItemType.ARTIST.value,
            Annotation.item_id == artist.id,
        )
    )

    return {
        "artist": artist_out(artist, annotation),
        "albums": albums_out(db, user, albums),
        "similar": [
            {
                "name": entry.name,
                "score": entry.score,
                "source": entry.source,
                "in_library": entry.in_library,
            }
            for entry in similar
        ],
    }


@router.get("/artists/{artist_id}/tracks", response_model=list[TrackOut])
def artist_tracks(
    artist_id: int,
    limit: int = Query(500, ge=1, le=2000),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tracks = list(
        db.scalars(
            select(Track)
            .where(Track.artist_id == artist_id)
            .order_by(Track.album_name, Track.disc_number, Track.track_number)
            .limit(limit)
        ).all()
    )
    return tracks_out(db, user, tracks)


# ─── Albums ────────────────────────────────────────────────────────────────


@router.get("/albums", response_model=list[AlbumOut])
def list_albums(
    q: str = Query(""),
    sort: str = Query("name", pattern="^(name|newest|year|artist|random|frequent|recent|starred)$"),
    genre: str = Query(""),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(Album)
    if q:
        stmt = stmt.where(
            or_(Album.name.ilike(f"%{q}%"), Album.album_artist.ilike(f"%{q}%"))
        )
    if genre:
        stmt = stmt.where(Album.genre.ilike(f"%{genre}%"))

    if sort in {"frequent", "recent", "starred"}:
        stmt = stmt.join(
            Annotation,
            and_(
                Annotation.item_id == Album.id,
                Annotation.item_type == ItemType.ALBUM.value,
                Annotation.user_id == user.id,
            ),
        )
        if sort == "frequent":
            stmt = stmt.where(Annotation.play_count > 0).order_by(Annotation.play_count.desc())
        elif sort == "recent":
            stmt = stmt.where(Annotation.play_date.isnot(None)).order_by(
                Annotation.play_date.desc()
            )
        else:
            stmt = stmt.where(Annotation.starred_at.isnot(None)).order_by(
                Annotation.starred_at.desc()
            )
    elif sort == "newest":
        stmt = stmt.order_by(Album.created_at.desc())
    elif sort == "year":
        stmt = stmt.order_by(Album.year.desc().nullslast(), Album.name)
    elif sort == "artist":
        stmt = stmt.order_by(Album.album_artist, Album.year.desc().nullslast())
    elif sort == "random":
        stmt = stmt.order_by(func.random())
    else:
        stmt = stmt.order_by(Album.sort_name, Album.name)

    albums = list(db.scalars(stmt.offset(offset).limit(limit)).all())
    return albums_out(db, user, albums)


@router.get("/albums/{album_id}")
def get_album(
    album_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found")

    tracks = list(
        db.scalars(
            select(Track)
            .where(Track.album_id == album.id)
            .order_by(Track.disc_number, Track.track_number, Track.title)
        ).all()
    )
    annotation = db.scalar(
        select(Annotation).where(
            Annotation.user_id == user.id,
            Annotation.item_type == ItemType.ALBUM.value,
            Annotation.item_id == album.id,
        )
    )
    return {
        "album": album_out(album, annotation),
        "description": album.description,
        "tracks": tracks_out(db, user, tracks),
    }


# ─── Tracks and search ─────────────────────────────────────────────────────


@router.get("/tracks", response_model=list[TrackOut])
def list_tracks(
    q: str = Query(""),
    genre: str = Query(""),
    sort: str = Query("title", pattern="^(title|artist|album|newest|random|frequent)$"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(Track)
    if q:
        stmt = stmt.where(
            or_(
                Track.title.ilike(f"%{q}%"),
                Track.artist_name.ilike(f"%{q}%"),
                Track.album_name.ilike(f"%{q}%"),
            )
        )
    if genre:
        stmt = stmt.where(Track.genre.ilike(f"%{genre}%"))

    if sort == "newest":
        stmt = stmt.order_by(Track.created_at.desc())
    elif sort == "random":
        stmt = stmt.order_by(func.random())
    elif sort == "artist":
        stmt = stmt.order_by(Track.artist_name, Track.album_name, Track.track_number)
    elif sort == "album":
        stmt = stmt.order_by(Track.album_name, Track.disc_number, Track.track_number)
    elif sort == "frequent":
        stmt = stmt.join(
            Annotation,
            and_(
                Annotation.item_id == Track.id,
                Annotation.item_type == ItemType.TRACK.value,
                Annotation.user_id == user.id,
            ),
        ).where(Annotation.play_count > 0).order_by(Annotation.play_count.desc())
    else:
        stmt = stmt.order_by(Track.title)

    tracks = list(db.scalars(stmt.offset(offset).limit(limit)).all())
    return tracks_out(db, user, tracks)


@router.get("/search", response_model=SearchResults)
def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pattern = f"%{q}%"
    artists = list(
        db.scalars(select(Artist).where(Artist.name.ilike(pattern)).limit(limit)).all()
    )
    albums = list(
        db.scalars(
            select(Album)
            .where(or_(Album.name.ilike(pattern), Album.album_artist.ilike(pattern)))
            .limit(limit)
        ).all()
    )
    tracks = list(
        db.scalars(
            select(Track)
            .where(
                or_(
                    Track.title.ilike(pattern),
                    Track.artist_name.ilike(pattern),
                    Track.album_name.ilike(pattern),
                )
            )
            .limit(limit)
        ).all()
    )
    return SearchResults(
        artists=artists_out(db, user, artists),
        albums=albums_out(db, user, albums),
        tracks=tracks_out(db, user, tracks),
    )


@router.get("/genres")
def list_genres(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.execute(
        select(Track.genre, func.count(Track.id))
        .where(Track.genre != "")
        .group_by(Track.genre)
        .order_by(func.count(Track.id).desc())
    ).all()
    return [{"name": name, "track_count": count} for name, count in rows]


@router.get("/stats")
def library_stats(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return {
        "artists": db.scalar(select(func.count(Artist.id))) or 0,
        "albums": db.scalar(select(func.count(Album.id))) or 0,
        "tracks": db.scalar(select(func.count(Track.id))) or 0,
        "duration": int(db.scalar(select(func.coalesce(func.sum(Track.duration), 0))) or 0),
        "size": int(db.scalar(select(func.coalesce(func.sum(Track.size), 0))) or 0),
        "plays": db.scalar(
            select(func.count(PlayHistory.id)).where(PlayHistory.user_id == user.id)
        ) or 0,
    }


# ─── Playback ──────────────────────────────────────────────────────────────


@router.get("/stream/{track_id}")
def stream_track(
    track_id: int,
    request: Request,
    format: str | None = Query(None),
    max_bitrate: int | None = Query(None, ge=0, le=1411),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user.stream_role:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Streaming disabled")

    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found")

    path = Path(track.path)
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Media file is missing from disk"
        )

    plan = transcode.plan_stream(
        track,
        user,
        requested_format=format or user.transcode_format,
        requested_bitrate=max_bitrate,
    )
    now_playing.update(user, track, "web")

    if not plan.transcode:
        file_size = path.stat().st_size
        byte_range = transcode.parse_range_header(request.headers.get("range"), file_size)
        if byte_range:
            start, end = byte_range
            return StreamingResponse(
                transcode.stream_direct(path, start, end),
                status_code=206,
                media_type=plan.content_type,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Content-Length": str(end - start + 1),
                    "Accept-Ranges": "bytes",
                },
            )
        return FileResponse(
            path, media_type=plan.content_type, headers={"Accept-Ranges": "bytes"}
        )

    try:
        generator = transcode.stream_transcoded(plan)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        )
    return StreamingResponse(
        generator,
        media_type=plan.content_type,
        headers={"Accept-Ranges": "none", "X-Content-Duration": str(track.duration)},
    )


@router.get("/cover/{kind}/{item_id}")
def cover_art(
    kind: str,
    item_id: int,
    size: int = Query(0, ge=0, le=2000),
    db: Session = Depends(get_db),
):
    """Cover art. Deliberately unauthenticated so <img> tags work without a token."""
    prefix = {"album": "al", "artist": "ar", "track": "tr", "playlist": "pl", "podcast": "pc"}.get(kind)
    if prefix is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown cover kind")

    path = _cover_path(db, f"{prefix}-{item_id}")
    if path is None:
        return Response(
            content=_placeholder_cover(f"{kind}:{item_id}", size or 300),
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    if size:
        import hashlib

        from ..config import settings

        cached = settings.cache_dir / "covers" / (
            hashlib.sha1(f"{path}:{size}".encode()).hexdigest() + ".jpg"
        )
        cached.parent.mkdir(parents=True, exist_ok=True)
        if not cached.exists():
            try:
                from PIL import Image

                with Image.open(path) as image:
                    if image.mode not in ("RGB", "L"):
                        image = image.convert("RGB")
                    image.thumbnail((size, size), Image.LANCZOS)
                    image.save(cached, "JPEG", quality=88)
            except Exception:
                return FileResponse(path)
        return FileResponse(cached, headers={"Cache-Control": "public, max-age=604800"})

    return FileResponse(path, headers={"Cache-Control": "public, max-age=604800"})


@router.post("/play/{track_id}", response_model=GenericResponse)
def record_play(
    track_id: int,
    submission: bool = Query(True),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Record a completed play (or a now-playing ping when submission=false)."""
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found")

    if submission:
        scrobble_service.record_play(db, user, track, client="web")
        return GenericResponse(message="Play recorded")

    now_playing.update(user, track, "web")
    scrobble_service.submit_now_playing(user, track)
    return GenericResponse(message="Now playing updated")


# ─── Annotations ───────────────────────────────────────────────────────────


@router.post("/star/{kind}/{item_id}", response_model=GenericResponse)
def star_item(
    kind: str,
    item_id: int,
    starred: bool = Query(True),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item_type = {"artist": ItemType.ARTIST, "album": ItemType.ALBUM, "track": ItemType.TRACK}.get(kind)
    if item_type is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown item kind")

    annotation = scrobble_service.get_or_create_annotation(
        db, user.id, item_type.value, item_id
    )
    annotation.starred_at = utcnow() if starred else None
    db.add(annotation)
    db.commit()
    return GenericResponse(message="Starred" if starred else "Unstarred")


@router.post("/rate/{kind}/{item_id}", response_model=GenericResponse)
def rate_item(
    kind: str,
    item_id: int,
    rating: int = Query(..., ge=0, le=5),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item_type = {"artist": ItemType.ARTIST, "album": ItemType.ALBUM, "track": ItemType.TRACK}.get(kind)
    if item_type is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown item kind")

    annotation = scrobble_service.get_or_create_annotation(
        db, user.id, item_type.value, item_id
    )
    annotation.rating = rating
    db.add(annotation)
    db.commit()
    return GenericResponse(message=f"Rated {rating}")


@router.get("/history", response_model=list[dict])
def play_history(
    limit: int = Query(50, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(PlayHistory)
        .where(PlayHistory.user_id == user.id)
        .order_by(PlayHistory.played_at.desc())
        .limit(limit)
    ).all()
    return [
        {
            "id": row.id,
            "track_id": row.track_id,
            "title": row.title,
            "artist_name": row.artist_name,
            "album_name": row.album_name,
            "played_at": row.played_at.isoformat(),
            "client": row.client,
        }
        for row in rows
    ]
