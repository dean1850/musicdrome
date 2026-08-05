"""Shared Subsonic plumbing: IDs, auth, response envelopes, entity serialisers.

Subsonic identifies everything with opaque strings, and ``getMusicDirectory``
takes an ID that may name either an artist or an album. Integer primary keys are
therefore exposed with a two-letter prefix (``ar-12``, ``al-7``, ``tr-341``) so
the type is recoverable from the ID alone.

Responses are rendered from plain dicts into either XML or JSON depending on the
``f`` parameter, following the Subsonic convention that scalars become
attributes and nested structures become child elements.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from typing import Any
from xml.etree import ElementTree

from fastapi import Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from .. import __subsonic_version__, __version__
from ..config import settings
from ..db import get_db, utcnow
from ..models import Album, Annotation, Artist, ItemType, Playlist, Track, User
from ..security import (
    decode_subsonic_password,
    decrypt_secret,
    verify_password,
    verify_subsonic_token,
)

log = logging.getLogger(__name__)

SERVER_TYPE = "musicdrome"

# OpenSubsonic extensions we actually implement
OPEN_SUBSONIC_EXTENSIONS = [
    {"name": "transcodeOffset", "versions": [1]},
    {"name": "formPost", "versions": [1]},
    {"name": "songLyrics", "versions": [1]},
]


# ─── Errors ────────────────────────────────────────────────────────────────


class SubsonicError(Exception):
    """Subsonic reports failures as HTTP 200 with an error payload."""

    GENERIC = 0
    MISSING_PARAMETER = 10
    CLIENT_TOO_OLD = 20
    SERVER_TOO_OLD = 30
    WRONG_CREDENTIALS = 40
    TOKEN_AUTH_UNSUPPORTED = 41
    NOT_AUTHORIZED = 50
    TRIAL_OVER = 60
    NOT_FOUND = 70

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ─── IDs ───────────────────────────────────────────────────────────────────

ARTIST = "ar"
ALBUM = "al"
TRACK = "tr"
PLAYLIST = "pl"
PODCAST = "pc"
EPISODE = "pe"
FOLDER = "fo"


def make_id(prefix: str, value: int | str) -> str:
    return f"{prefix}-{value}"


def parse_id(raw: str | None, expected: str | None = None) -> tuple[str, int]:
    """Split ``"al-12"`` into ``("al", 12)``.

    Bare numeric IDs are tolerated — some older clients round-trip them — and
    resolve to ``expected`` when the caller knows the type.
    """
    if raw is None or raw == "":
        raise SubsonicError(SubsonicError.MISSING_PARAMETER, "Required parameter 'id' is missing")

    text = str(raw)
    if "-" in text:
        prefix, _, number = text.partition("-")
        if prefix in {ARTIST, ALBUM, TRACK, PLAYLIST, PODCAST, EPISODE, FOLDER}:
            try:
                return prefix, int(number)
            except ValueError:
                raise SubsonicError(SubsonicError.NOT_FOUND, f"Malformed id: {raw}")

    try:
        return (expected or TRACK), int(text)
    except ValueError:
        raise SubsonicError(SubsonicError.NOT_FOUND, f"Malformed id: {raw}")


def parse_typed_id(raw: str | None, expected: str) -> int:
    prefix, value = parse_id(raw, expected)
    if prefix != expected:
        raise SubsonicError(
            SubsonicError.NOT_FOUND, f"Expected a {expected} id, got '{raw}'"
        )
    return value


# ─── Rendering ─────────────────────────────────────────────────────────────


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return str(value)


def _to_xml(name: str, payload: dict) -> ElementTree.Element:
    element = ElementTree.Element(name)
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, dict):
            element.append(_to_xml(key, value))
        elif isinstance(value, list):
            for entry in value:
                if isinstance(entry, dict):
                    element.append(_to_xml(key, entry))
                else:
                    child = ElementTree.SubElement(element, key)
                    child.text = _stringify(entry)
        else:
            element.set(key, _stringify(value))
    return element


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, datetime):
        return _stringify(value)
    return value


def render(
    body: dict | None = None,
    *,
    fmt: str = "xml",
    callback: str | None = None,
    error: SubsonicError | None = None,
) -> Response:
    """Wrap a payload in the ``subsonic-response`` envelope."""
    envelope: dict[str, Any] = {
        "status": "failed" if error else "ok",
        "version": __subsonic_version__,
        "type": SERVER_TYPE,
        "serverVersion": __version__,
        "openSubsonic": True,
    }
    if error:
        envelope["error"] = {"code": error.code, "message": error.message}
    elif body:
        envelope.update(body)

    fmt = (fmt or "xml").lower()

    if fmt in {"json", "jsonp"}:
        document = {"subsonic-response": _json_safe(envelope)}
        text = json.dumps(document, ensure_ascii=False)
        if fmt == "jsonp" and callback:
            return Response(
                content=f"{callback}({text});", media_type="text/javascript; charset=utf-8"
            )
        return Response(content=text, media_type="application/json; charset=utf-8")

    root = _to_xml("subsonic-response", envelope)
    root.set("xmlns", "http://subsonic.org/restapi")
    xml = ElementTree.tostring(root, encoding="unicode")
    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?>\n{xml}',
        media_type="text/xml; charset=utf-8",
    )


# ─── Routing helper ────────────────────────────────────────────────────────


def endpoint(router, name: str):
    """Register a handler at ``/name`` and ``/name.view``.

    Clients are split on whether they append ``.view`` to Subsonic verbs, and
    both spellings have to work. Registering explicitly (rather than matching a
    wildcard) keeps unknown verbs returning a proper Subsonic error.
    """

    def decorator(func):
        for path, suffix in ((f"/{name}", ""), (f"/{name}.view", "_view")):
            router.add_api_route(
                path,
                func,
                methods=["GET", "POST"],
                name=f"subsonic_{name}{suffix}",
                include_in_schema=not suffix,
                response_model=None,
            )
        return func

    return decorator


# ─── Request context ───────────────────────────────────────────────────────


class SubsonicContext:
    """Everything a Subsonic handler needs: the caller, the DB, the format."""

    def __init__(self, user: User, db: Session, fmt: str, callback: str | None, client: str) -> None:
        self.user = user
        self.db = db
        self.fmt = fmt
        self.callback = callback
        self.client = client

    def ok(self, body: dict | None = None) -> Response:
        return render(body, fmt=self.fmt, callback=self.callback)

    def fail(self, code: int, message: str) -> Response:
        return render(fmt=self.fmt, callback=self.callback, error=SubsonicError(code, message))


async def _read_params(request: Request) -> dict[str, str]:
    """Subsonic clients may send parameters as query string or form body."""
    params = dict(request.query_params)
    if request.method == "POST":
        try:
            form = await request.form()
            for key, value in form.items():
                params.setdefault(key, str(value))
        except Exception:
            pass
    return params


def authenticate(db: Session, params: dict[str, str]) -> User:
    username = params.get("u") or ""
    if not username:
        raise SubsonicError(SubsonicError.MISSING_PARAMETER, "Required parameter 'u' is missing")

    user = db.scalar(select(User).where(User.username == username))
    if user is None or not user.is_active:
        raise SubsonicError(SubsonicError.WRONG_CREDENTIALS, "Wrong username or password")

    token = params.get("t")
    salt = params.get("s")
    password = params.get("p")

    if token and salt:
        cleartext = decrypt_secret(user.password_enc)
        if not cleartext:
            raise SubsonicError(
                SubsonicError.TOKEN_AUTH_UNSUPPORTED,
                "Token authentication is unavailable for this account — "
                "sign in on the web UI once to enable it",
            )
        if not verify_subsonic_token(cleartext, salt, token):
            raise SubsonicError(SubsonicError.WRONG_CREDENTIALS, "Wrong username or password")
        return user

    if password:
        if settings.subsonic_require_token_auth:
            raise SubsonicError(
                SubsonicError.NOT_AUTHORIZED,
                "This server requires token authentication",
            )
        cleartext = decode_subsonic_password(password)
        if not verify_password(cleartext, user.password_hash):
            raise SubsonicError(SubsonicError.WRONG_CREDENTIALS, "Wrong username or password")
        return user

    raise SubsonicError(
        SubsonicError.MISSING_PARAMETER, "Required parameter 't'/'s' or 'p' is missing"
    )


async def get_context(
    request: Request,
    db: Session = Depends(get_db),
    f: str = Query("xml"),
    callback: str | None = Query(None),
) -> SubsonicContext:
    params = await _read_params(request)
    fmt = params.get("f", f) or "xml"
    cb = params.get("callback", callback)
    user = authenticate(db, params)
    request.state.subsonic_params = params
    return SubsonicContext(user, db, fmt, cb, params.get("c", ""))


def params_of(request: Request) -> dict[str, str]:
    return getattr(request.state, "subsonic_params", dict(request.query_params))


def param_int(params: dict[str, str], name: str, default: int | None = None) -> int | None:
    raw = params.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def param_bool(params: dict[str, str], name: str, default: bool = False) -> bool:
    raw = params.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


# ─── Annotations ───────────────────────────────────────────────────────────


def annotation_for(db: Session, user_id: int, item_type: str, item_id: int) -> Annotation | None:
    return db.scalar(
        select(Annotation).where(
            Annotation.user_id == user_id,
            Annotation.item_type == item_type,
            Annotation.item_id == item_id,
        )
    )


def annotations_map(
    db: Session, user_id: int, item_type: str, item_ids: list[int]
) -> dict[int, Annotation]:
    """Bulk-load annotations so list endpoints don't fire a query per row."""
    if not item_ids:
        return {}
    rows = db.scalars(
        select(Annotation).where(
            Annotation.user_id == user_id,
            Annotation.item_type == item_type,
            Annotation.item_id.in_(item_ids),
        )
    ).all()
    return {row.item_id: row for row in rows}


# ─── Entity serialisers ────────────────────────────────────────────────────


def artist_dict(artist: Artist, annotation: Annotation | None = None) -> dict:
    payload = {
        "id": make_id(ARTIST, artist.id),
        "name": artist.name,
        "albumCount": artist.album_count,
        "songCount": artist.track_count,
        "coverArt": make_id(ARTIST, artist.id) if artist.image_path else None,
        "artistImageUrl": artist.image_url or None,
        "musicBrainzId": artist.mbid or None,
        "sortName": artist.sort_name or None,
    }
    if annotation:
        if annotation.starred_at:
            payload["starred"] = annotation.starred_at
        if annotation.rating:
            payload["userRating"] = annotation.rating
    return payload


def album_dict(album: Album, annotation: Annotation | None = None, *, id3: bool = True) -> dict:
    payload = {
        "id": make_id(ALBUM, album.id),
        "name" if id3 else "title": album.name,
        "album": album.name,
        "artist": album.album_artist or album.artist_name,
        "artistId": make_id(ARTIST, album.artist_id) if album.artist_id else None,
        "coverArt": make_id(ALBUM, album.id),
        "songCount": album.song_count,
        "duration": album.duration,
        "created": album.created_at,
        "year": album.year,
        "genre": album.genre or None,
        "isDir": True,
        "parent": make_id(ARTIST, album.artist_id) if album.artist_id else None,
        "musicBrainzId": album.mbid or None,
        "isCompilation": album.compilation or None,
    }
    if annotation:
        if annotation.starred_at:
            payload["starred"] = annotation.starred_at
        if annotation.rating:
            payload["userRating"] = annotation.rating
        if annotation.play_count:
            payload["playCount"] = annotation.play_count
    return {k: v for k, v in payload.items() if v is not None}


def track_dict(track: Track, annotation: Annotation | None = None) -> dict:
    payload = {
        "id": make_id(TRACK, track.id),
        "parent": make_id(ALBUM, track.album_id) if track.album_id else None,
        "isDir": False,
        "title": track.title,
        "album": track.album_name,
        "artist": track.artist_name,
        "track": track.track_number or None,
        "year": track.year,
        "genre": track.genre or None,
        "coverArt": make_id(ALBUM, track.album_id) if track.album_id else make_id(TRACK, track.id),
        "size": track.size,
        "contentType": track.content_type,
        "suffix": track.suffix,
        "duration": track.duration,
        "bitRate": track.bitrate or None,
        "samplingRate": track.sample_rate or None,
        "channelCount": track.channels or None,
        "path": track.path,
        "discNumber": track.disc_number or None,
        "albumId": make_id(ALBUM, track.album_id) if track.album_id else None,
        "artistId": make_id(ARTIST, track.artist_id) if track.artist_id else None,
        "type": "music",
        "isVideo": False,
        "created": track.created_at,
        "musicBrainzId": track.mbid or None,
        "bpm": track.bpm or None,
        "sortName": track.sort_title or None,
    }
    if annotation:
        if annotation.starred_at:
            payload["starred"] = annotation.starred_at
        if annotation.rating:
            payload["userRating"] = annotation.rating
        if annotation.play_count:
            payload["playCount"] = annotation.play_count
        if annotation.play_date:
            payload["played"] = annotation.play_date
    return {k: v for k, v in payload.items() if v is not None}


def tracks_payload(db: Session, user: User, tracks: list[Track]) -> list[dict]:
    annotations = annotations_map(
        db, user.id, ItemType.TRACK.value, [t.id for t in tracks]
    )
    return [track_dict(track, annotations.get(track.id)) for track in tracks]


def albums_payload(db: Session, user: User, albums: list[Album], *, id3: bool = True) -> list[dict]:
    annotations = annotations_map(
        db, user.id, ItemType.ALBUM.value, [a.id for a in albums]
    )
    return [album_dict(album, annotations.get(album.id), id3=id3) for album in albums]


def artists_payload(db: Session, user: User, artists: list[Artist]) -> list[dict]:
    annotations = annotations_map(
        db, user.id, ItemType.ARTIST.value, [a.id for a in artists]
    )
    return [artist_dict(artist, annotations.get(artist.id)) for artist in artists]


def playlist_dict(playlist: Playlist, owner: User | None = None) -> dict:
    return {
        "id": make_id(PLAYLIST, playlist.id),
        "name": playlist.name,
        "comment": playlist.comment or None,
        "owner": owner.username if owner else None,
        "public": playlist.public,
        "songCount": playlist.song_count,
        "duration": playlist.duration,
        "created": playlist.created_at,
        "changed": playlist.updated_at,
        "coverArt": make_id(PLAYLIST, playlist.id) if playlist.cover_art_path else None,
    }


def user_dict(user: User) -> dict:
    return {
        "username": user.username,
        "email": user.email or None,
        "scrobblingEnabled": user.lastfm_enabled or user.listenbrainz_enabled,
        "adminRole": user.is_admin,
        "settingsRole": user.is_admin,
        "downloadRole": user.download_role,
        "uploadRole": user.upload_role,
        "playlistRole": user.playlist_role,
        "coverArtRole": user.cover_art_role,
        "commentRole": user.comment_role,
        "podcastRole": user.podcast_role,
        "streamRole": user.stream_role,
        "jukeboxRole": user.jukebox_role,
        "shareRole": user.share_role,
        "videoConversionRole": False,
        "maxBitRate": user.max_bitrate or None,
        "folder": [0],
    }


# ─── Now playing ───────────────────────────────────────────────────────────


class NowPlayingRegistry:
    """In-memory 'currently playing' state, as Subsonic's getNowPlaying expects."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[int, dict] = {}

    def update(self, user: User, track: Track, client: str = "") -> None:
        with self._lock:
            self._entries[user.id] = {
                "user": user.username,
                "track_id": track.id,
                "client": client,
                "at": utcnow(),
            }

    def entries(self, max_age_minutes: int = 10) -> list[dict]:
        cutoff = utcnow()
        with self._lock:
            live = []
            for entry in self._entries.values():
                minutes = int((cutoff - entry["at"]).total_seconds() // 60)
                if minutes <= max_age_minutes:
                    live.append({**entry, "minutes_ago": minutes})
            return live


now_playing = NowPlayingRegistry()


def starred_payload(db: Session, user: User) -> dict:
    """Shared body for getStarred and getStarred2."""
    starred = db.scalars(
        select(Annotation).where(
            Annotation.user_id == user.id, Annotation.starred_at.isnot(None)
        )
    ).all()

    artist_ids = [a.item_id for a in starred if a.item_type == ItemType.ARTIST.value]
    album_ids = [a.item_id for a in starred if a.item_type == ItemType.ALBUM.value]
    track_ids = [a.item_id for a in starred if a.item_type == ItemType.TRACK.value]

    artists = (
        db.scalars(select(Artist).where(Artist.id.in_(artist_ids))).all()
        if artist_ids else []
    )
    albums = (
        db.scalars(select(Album).where(Album.id.in_(album_ids))).all()
        if album_ids else []
    )
    tracks = (
        db.scalars(select(Track).where(Track.id.in_(track_ids))).all()
        if track_ids else []
    )

    return {
        "artist": artists_payload(db, user, list(artists)),
        "album": albums_payload(db, user, list(albums)),
        "song": tracks_payload(db, user, list(tracks)),
    }


def track_query_with_annotation(user_id: int):
    """Select Track joined to this user's annotation, for sorted list endpoints."""
    return select(Track, Annotation).outerjoin(
        Annotation,
        and_(
            Annotation.item_id == Track.id,
            Annotation.item_type == ItemType.TRACK.value,
            Annotation.user_id == user_id,
        ),
    )
