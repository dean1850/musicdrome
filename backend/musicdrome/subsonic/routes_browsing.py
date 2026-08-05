"""Subsonic browsing, search, rating and scrobbling endpoints."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import and_, func, or_, select

from ..db import utcnow
from ..models import (
    Album,
    Annotation,
    Artist,
    Genre,
    ItemType,
    PlayHistory,
    SimilarArtist,
    Track,
)
from ..services import scrobble as scrobble_service
from ..services.lastfm import lastfm
from .common import (
    ALBUM,
    ARTIST,
    TRACK,
    SubsonicContext,
    SubsonicError,
    album_dict,
    albums_payload,
    annotation_for,
    annotations_map,
    artist_dict,
    artists_payload,
    endpoint,
    get_context,
    make_id,
    now_playing,
    param_bool,
    param_int,
    params_of,
    parse_id,
    parse_typed_id,
    starred_payload,
    track_dict,
    track_query_with_annotation,
    tracks_payload,
)

log = logging.getLogger(__name__)

router = APIRouter()


def _index_letter(artist: Artist) -> str:
    source = (artist.sort_name or artist.name or "#").strip()
    first = source[0].upper() if source else "#"
    return first if first.isalpha() else "#"


def _indexes_body(ctx: SubsonicContext) -> dict:
    artists = ctx.db.scalars(select(Artist).order_by(Artist.sort_name, Artist.name)).all()
    annotations = annotations_map(
        ctx.db, ctx.user.id, ItemType.ARTIST.value, [a.id for a in artists]
    )

    buckets: dict[str, list[dict]] = {}
    for artist in artists:
        buckets.setdefault(_index_letter(artist), []).append(
            artist_dict(artist, annotations.get(artist.id))
        )

    return {
        "index": [
            {"name": letter, "artist": buckets[letter]}
            for letter in sorted(buckets, key=lambda x: (x == "#", x))
        ]
    }


# ─── Directory-style browsing ──────────────────────────────────────────────


@endpoint(router, "getIndexes")
def get_indexes(ctx: SubsonicContext = Depends(get_context)):
    body = _indexes_body(ctx)
    body["lastModified"] = int(utcnow().timestamp() * 1000)
    body["ignoredArticles"] = "The El La Los Las Le Les"
    return ctx.ok({"indexes": body})


@endpoint(router, "getArtists")
def get_artists(ctx: SubsonicContext = Depends(get_context)):
    body = _indexes_body(ctx)
    body["ignoredArticles"] = "The El La Los Las Le Les"
    return ctx.ok({"artists": body})


@endpoint(router, "getMusicDirectory")
def get_music_directory(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    prefix, item_id = parse_id(params.get("id"), ARTIST)

    if prefix == ARTIST:
        artist = ctx.db.get(Artist, item_id)
        if artist is None:
            raise SubsonicError(SubsonicError.NOT_FOUND, "Artist not found")
        albums = ctx.db.scalars(
            select(Album)
            .where(Album.artist_id == artist.id)
            .order_by(Album.year.desc().nullslast(), Album.name)
        ).all()
        return ctx.ok(
            {
                "directory": {
                    "id": make_id(ARTIST, artist.id),
                    "name": artist.name,
                    "child": albums_payload(ctx.db, ctx.user, list(albums), id3=False),
                }
            }
        )

    if prefix == ALBUM:
        album = ctx.db.get(Album, item_id)
        if album is None:
            raise SubsonicError(SubsonicError.NOT_FOUND, "Album not found")
        tracks = ctx.db.scalars(
            select(Track)
            .where(Track.album_id == album.id)
            .order_by(Track.disc_number, Track.track_number, Track.title)
        ).all()
        return ctx.ok(
            {
                "directory": {
                    "id": make_id(ALBUM, album.id),
                    "parent": make_id(ARTIST, album.artist_id) if album.artist_id else None,
                    "name": album.name,
                    "child": tracks_payload(ctx.db, ctx.user, list(tracks)),
                }
            }
        )

    raise SubsonicError(SubsonicError.NOT_FOUND, "Directory not found")


# ─── ID3 browsing ──────────────────────────────────────────────────────────


@endpoint(router, "getArtist")
def get_artist(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    artist_id = parse_typed_id(params.get("id"), ARTIST)

    artist = ctx.db.get(Artist, artist_id)
    if artist is None:
        raise SubsonicError(SubsonicError.NOT_FOUND, "Artist not found")

    albums = ctx.db.scalars(
        select(Album)
        .where(Album.artist_id == artist.id)
        .order_by(Album.year.desc().nullslast(), Album.name)
    ).all()

    payload = artist_dict(
        artist, annotation_for(ctx.db, ctx.user.id, ItemType.ARTIST.value, artist.id)
    )
    payload["album"] = albums_payload(ctx.db, ctx.user, list(albums))
    return ctx.ok({"artist": payload})


@endpoint(router, "getAlbum")
def get_album(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    album_id = parse_typed_id(params.get("id"), ALBUM)

    album = ctx.db.get(Album, album_id)
    if album is None:
        raise SubsonicError(SubsonicError.NOT_FOUND, "Album not found")

    tracks = ctx.db.scalars(
        select(Track)
        .where(Track.album_id == album.id)
        .order_by(Track.disc_number, Track.track_number, Track.title)
    ).all()

    payload = album_dict(
        album, annotation_for(ctx.db, ctx.user.id, ItemType.ALBUM.value, album.id)
    )
    payload["song"] = tracks_payload(ctx.db, ctx.user, list(tracks))
    return ctx.ok({"album": payload})


@endpoint(router, "getSong")
def get_song(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    track_id = parse_typed_id(params.get("id"), TRACK)

    track = ctx.db.get(Track, track_id)
    if track is None:
        raise SubsonicError(SubsonicError.NOT_FOUND, "Song not found")

    annotation = annotation_for(ctx.db, ctx.user.id, ItemType.TRACK.value, track.id)
    return ctx.ok({"song": track_dict(track, annotation)})


@endpoint(router, "getGenres")
def get_genres(ctx: SubsonicContext = Depends(get_context)):
    rows = ctx.db.execute(
        select(
            Track.genre,
            func.count(Track.id),
            func.count(func.distinct(Track.album_id)),
        )
        .where(Track.genre != "")
        .group_by(Track.genre)
        .order_by(func.count(Track.id).desc())
    ).all()

    return ctx.ok(
        {
            "genres": {
                "genre": [
                    {"value": name, "songCount": songs, "albumCount": albums}
                    for name, songs, albums in rows
                ]
            }
        }
    )


# ─── Info / similarity ─────────────────────────────────────────────────────


def _artist_info_body(ctx: SubsonicContext, artist: Artist, count: int, id3: bool) -> dict:
    similar = ctx.db.scalars(
        select(SimilarArtist)
        .where(SimilarArtist.artist_id == artist.id)
        .order_by(SimilarArtist.score.desc())
        .limit(count)
    ).all()

    entries = []
    for entry in similar:
        match = ctx.db.scalar(select(Artist).where(Artist.name == entry.name))
        if match is not None:
            entries.append(artist_dict(match))
        elif not id3:
            entries.append({"id": "-1", "name": entry.name})

    image = artist.image_url or ""
    return {
        "biography": artist.biography or "",
        "musicBrainzId": artist.mbid or None,
        "lastFmUrl": artist.lastfm_url or None,
        "smallImageUrl": image or None,
        "mediumImageUrl": image or None,
        "largeImageUrl": image or None,
        "similarArtist": entries,
    }


@endpoint(router, "getArtistInfo")
def get_artist_info(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    _prefix, artist_id = parse_id(params.get("id"), ARTIST)
    artist = ctx.db.get(Artist, artist_id)
    if artist is None:
        raise SubsonicError(SubsonicError.NOT_FOUND, "Artist not found")

    count = param_int(params, "count", 20) or 20
    return ctx.ok({"artistInfo": _artist_info_body(ctx, artist, count, id3=False)})


@endpoint(router, "getArtistInfo2")
def get_artist_info2(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    artist_id = parse_typed_id(params.get("id"), ARTIST)
    artist = ctx.db.get(Artist, artist_id)
    if artist is None:
        raise SubsonicError(SubsonicError.NOT_FOUND, "Artist not found")

    count = param_int(params, "count", 20) or 20
    return ctx.ok({"artistInfo2": _artist_info_body(ctx, artist, count, id3=True)})


def _album_info_body(album: Album) -> dict:
    return {
        "notes": album.description or "",
        "musicBrainzId": album.mbid or None,
        "smallImageUrl": None,
        "mediumImageUrl": None,
        "largeImageUrl": None,
    }


@endpoint(router, "getAlbumInfo")
def get_album_info(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    _prefix, album_id = parse_id(params.get("id"), ALBUM)
    album = ctx.db.get(Album, album_id)
    if album is None:
        raise SubsonicError(SubsonicError.NOT_FOUND, "Album not found")
    return ctx.ok({"albumInfo": _album_info_body(album)})


@endpoint(router, "getAlbumInfo2")
def get_album_info2(request: Request, ctx: SubsonicContext = Depends(get_context)):
    return get_album_info(request, ctx)


def _similar_songs(ctx: SubsonicContext, artist_names: list[str], count: int) -> list[Track]:
    if not artist_names:
        return []
    conditions = [Track.artist_name.ilike(name) for name in artist_names]
    return list(
        ctx.db.scalars(
            select(Track).where(or_(*conditions)).order_by(func.random()).limit(count)
        ).all()
    )


def _similar_names(ctx: SubsonicContext, artist: Artist, limit: int = 20) -> list[str]:
    names = [
        row.name
        for row in ctx.db.scalars(
            select(SimilarArtist)
            .where(SimilarArtist.artist_id == artist.id, SimilarArtist.in_library.is_(True))
            .order_by(SimilarArtist.score.desc())
            .limit(limit)
        ).all()
    ]
    return names or [artist.name]


def _resolve_artist(ctx: SubsonicContext, raw_id: str | None) -> Artist:
    prefix, item_id = parse_id(raw_id, ARTIST)
    if prefix == ARTIST:
        artist = ctx.db.get(Artist, item_id)
    elif prefix == ALBUM:
        album = ctx.db.get(Album, item_id)
        artist = ctx.db.get(Artist, album.artist_id) if album and album.artist_id else None
    else:
        track = ctx.db.get(Track, item_id)
        artist = ctx.db.get(Artist, track.artist_id) if track and track.artist_id else None

    if artist is None:
        raise SubsonicError(SubsonicError.NOT_FOUND, "Artist not found")
    return artist


@endpoint(router, "getSimilarSongs")
def get_similar_songs(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    artist = _resolve_artist(ctx, params.get("id"))
    count = param_int(params, "count", 50) or 50
    tracks = _similar_songs(ctx, _similar_names(ctx, artist), count)
    return ctx.ok({"similarSongs": {"song": tracks_payload(ctx.db, ctx.user, tracks)}})


@endpoint(router, "getSimilarSongs2")
def get_similar_songs2(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    artist = _resolve_artist(ctx, params.get("id"))
    count = param_int(params, "count", 50) or 50
    tracks = _similar_songs(ctx, _similar_names(ctx, artist), count)
    return ctx.ok({"similarSongs2": {"song": tracks_payload(ctx.db, ctx.user, tracks)}})


@endpoint(router, "getTopSongs")
def get_top_songs(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    name = params.get("artist", "")
    count = param_int(params, "count", 50) or 50
    if not name:
        raise SubsonicError(SubsonicError.MISSING_PARAMETER, "Parameter 'artist' is missing")

    ordered_titles: list[str] = []
    if lastfm.configured:
        ordered_titles = [entry["title"] for entry in lastfm.top_tracks(name, limit=count)]

    owned = list(
        ctx.db.scalars(
            select(Track).where(Track.artist_name.ilike(name)).limit(500)
        ).all()
    )
    if ordered_titles:
        rank = {title.lower(): index for index, title in enumerate(ordered_titles)}
        owned.sort(key=lambda t: rank.get(t.title.lower(), 10_000))
    else:
        annotations = annotations_map(
            ctx.db, ctx.user.id, ItemType.TRACK.value, [t.id for t in owned]
        )
        owned.sort(
            key=lambda t: annotations[t.id].play_count if t.id in annotations else 0,
            reverse=True,
        )

    return ctx.ok(
        {"topSongs": {"song": tracks_payload(ctx.db, ctx.user, owned[:count])}}
    )


# ─── Lists ─────────────────────────────────────────────────────────────────


def _album_list(ctx: SubsonicContext, params: dict) -> list[Album]:
    list_type = params.get("type", "alphabeticalByName")
    size = min(param_int(params, "size", 10) or 10, 500)
    offset = param_int(params, "offset", 0) or 0

    stmt = select(Album)

    if list_type == "random":
        stmt = stmt.order_by(func.random())
    elif list_type == "newest":
        stmt = stmt.order_by(Album.created_at.desc())
    elif list_type == "alphabeticalByName":
        stmt = stmt.order_by(Album.sort_name, Album.name)
    elif list_type == "alphabeticalByArtist":
        stmt = stmt.order_by(Album.album_artist, Album.year.desc().nullslast(), Album.name)
    elif list_type == "byYear":
        from_year = param_int(params, "fromYear", 0) or 0
        to_year = param_int(params, "toYear", 9999) or 9999
        low, high = min(from_year, to_year), max(from_year, to_year)
        stmt = stmt.where(Album.year.between(low, high))
        # Subsonic reverses the ordering when fromYear > toYear
        stmt = stmt.order_by(
            Album.year.desc() if from_year > to_year else Album.year.asc()
        )
    elif list_type == "byGenre":
        genre = params.get("genre", "")
        if not genre:
            raise SubsonicError(
                SubsonicError.MISSING_PARAMETER, "Parameter 'genre' is required for type=byGenre"
            )
        stmt = stmt.where(Album.genre.ilike(f"%{genre}%")).order_by(Album.name)
    elif list_type in {"starred", "highest", "frequent", "recent"}:
        item_type = ItemType.ALBUM.value
        stmt = stmt.join(
            Annotation,
            and_(
                Annotation.item_id == Album.id,
                Annotation.item_type == item_type,
                Annotation.user_id == ctx.user.id,
            ),
        )
        if list_type == "starred":
            stmt = stmt.where(Annotation.starred_at.isnot(None)).order_by(
                Annotation.starred_at.desc()
            )
        elif list_type == "highest":
            stmt = stmt.where(Annotation.rating > 0).order_by(Annotation.rating.desc())
        elif list_type == "frequent":
            stmt = stmt.where(Annotation.play_count > 0).order_by(
                Annotation.play_count.desc()
            )
        else:
            stmt = stmt.where(Annotation.play_date.isnot(None)).order_by(
                Annotation.play_date.desc()
            )
    else:
        stmt = stmt.order_by(Album.sort_name, Album.name)

    return list(ctx.db.scalars(stmt.offset(offset).limit(size)).all())


@endpoint(router, "getAlbumList")
def get_album_list(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    albums = _album_list(ctx, params)
    return ctx.ok(
        {"albumList": {"album": albums_payload(ctx.db, ctx.user, albums, id3=False)}}
    )


@endpoint(router, "getAlbumList2")
def get_album_list2(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    albums = _album_list(ctx, params)
    return ctx.ok({"albumList2": {"album": albums_payload(ctx.db, ctx.user, albums)}})


@endpoint(router, "getRandomSongs")
def get_random_songs(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    size = min(param_int(params, "size", 10) or 10, 500)

    stmt = select(Track)
    if params.get("genre"):
        stmt = stmt.where(Track.genre.ilike(f"%{params['genre']}%"))
    from_year = param_int(params, "fromYear")
    to_year = param_int(params, "toYear")
    if from_year is not None:
        stmt = stmt.where(Track.year >= from_year)
    if to_year is not None:
        stmt = stmt.where(Track.year <= to_year)

    tracks = list(ctx.db.scalars(stmt.order_by(func.random()).limit(size)).all())
    return ctx.ok({"randomSongs": {"song": tracks_payload(ctx.db, ctx.user, tracks)}})


@endpoint(router, "getSongsByGenre")
def get_songs_by_genre(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    genre = params.get("genre")
    if not genre:
        raise SubsonicError(SubsonicError.MISSING_PARAMETER, "Parameter 'genre' is missing")

    size = min(param_int(params, "count", 10) or 10, 500)
    offset = param_int(params, "offset", 0) or 0
    tracks = list(
        ctx.db.scalars(
            select(Track)
            .where(Track.genre.ilike(f"%{genre}%"))
            .order_by(Track.artist_name, Track.album_name, Track.track_number)
            .offset(offset)
            .limit(size)
        ).all()
    )
    return ctx.ok({"songsByGenre": {"song": tracks_payload(ctx.db, ctx.user, tracks)}})


@endpoint(router, "getStarred")
def get_starred(ctx: SubsonicContext = Depends(get_context)):
    return ctx.ok({"starred": starred_payload(ctx.db, ctx.user)})


@endpoint(router, "getStarred2")
def get_starred2(ctx: SubsonicContext = Depends(get_context)):
    return ctx.ok({"starred2": starred_payload(ctx.db, ctx.user)})


# ─── Search ────────────────────────────────────────────────────────────────


def _search(ctx: SubsonicContext, params: dict) -> dict:
    query = (params.get("query") or "").strip().strip('"')
    artist_count = param_int(params, "artistCount", 20) or 20
    artist_offset = param_int(params, "artistOffset", 0) or 0
    album_count = param_int(params, "albumCount", 20) or 20
    album_offset = param_int(params, "albumOffset", 0) or 0
    song_count = param_int(params, "songCount", 20) or 20
    song_offset = param_int(params, "songOffset", 0) or 0

    # An empty query means "everything" in the Subsonic spec
    pattern = f"%{query}%" if query else "%"

    artists = list(
        ctx.db.scalars(
            select(Artist)
            .where(Artist.name.ilike(pattern))
            .order_by(Artist.name)
            .offset(artist_offset)
            .limit(artist_count)
        ).all()
    )
    albums = list(
        ctx.db.scalars(
            select(Album)
            .where(or_(Album.name.ilike(pattern), Album.album_artist.ilike(pattern)))
            .order_by(Album.name)
            .offset(album_offset)
            .limit(album_count)
        ).all()
    )
    tracks = list(
        ctx.db.scalars(
            select(Track)
            .where(
                or_(
                    Track.title.ilike(pattern),
                    Track.artist_name.ilike(pattern),
                    Track.album_name.ilike(pattern),
                )
            )
            .order_by(Track.artist_name, Track.title)
            .offset(song_offset)
            .limit(song_count)
        ).all()
    )

    return {
        "artist": artists_payload(ctx.db, ctx.user, artists),
        "album": albums_payload(ctx.db, ctx.user, albums),
        "song": tracks_payload(ctx.db, ctx.user, tracks),
    }


@endpoint(router, "search2")
def search2(request: Request, ctx: SubsonicContext = Depends(get_context)):
    return ctx.ok({"searchResult2": _search(ctx, params_of(request))})


@endpoint(router, "search3")
def search3(request: Request, ctx: SubsonicContext = Depends(get_context)):
    return ctx.ok({"searchResult3": _search(ctx, params_of(request))})


@endpoint(router, "search")
def search(request: Request, ctx: SubsonicContext = Depends(get_context)):
    """The pre-1.4.0 search verb, kept for very old clients."""
    params = dict(params_of(request))
    params.setdefault("query", params.get("any") or params.get("title") or "")
    result = _search(ctx, params)
    return ctx.ok({"searchResult": {"match": result["song"]}})


# ─── Annotation verbs ──────────────────────────────────────────────────────


def _annotation_targets(request: Request, params: dict) -> list[tuple[str, int]]:
    """Collect (item_type, id) pairs from id / albumId / artistId parameters."""
    targets: list[tuple[str, int]] = []
    query = request.query_params

    for raw in query.getlist("id") or ([params["id"]] if params.get("id") else []):
        prefix, value = parse_id(raw, TRACK)
        mapping = {
            ARTIST: ItemType.ARTIST.value,
            ALBUM: ItemType.ALBUM.value,
            TRACK: ItemType.TRACK.value,
        }
        if prefix in mapping:
            targets.append((mapping[prefix], value))

    for raw in query.getlist("albumId") or (
        [params["albumId"]] if params.get("albumId") else []
    ):
        targets.append((ItemType.ALBUM.value, parse_id(raw, ALBUM)[1]))

    for raw in query.getlist("artistId") or (
        [params["artistId"]] if params.get("artistId") else []
    ):
        targets.append((ItemType.ARTIST.value, parse_id(raw, ARTIST)[1]))

    if not targets:
        raise SubsonicError(SubsonicError.MISSING_PARAMETER, "No item id supplied")
    return targets


def _set_starred(ctx: SubsonicContext, targets: list[tuple[str, int]], starred: bool) -> None:
    for item_type, item_id in targets:
        annotation = scrobble_service.get_or_create_annotation(
            ctx.db, ctx.user.id, item_type, item_id
        )
        annotation.starred_at = utcnow() if starred else None
        ctx.db.add(annotation)
    ctx.db.commit()


@endpoint(router, "star")
def star(request: Request, ctx: SubsonicContext = Depends(get_context)):
    _set_starred(ctx, _annotation_targets(request, params_of(request)), True)
    return ctx.ok()


@endpoint(router, "unstar")
def unstar(request: Request, ctx: SubsonicContext = Depends(get_context)):
    _set_starred(ctx, _annotation_targets(request, params_of(request)), False)
    return ctx.ok()


@endpoint(router, "setRating")
def set_rating(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    rating = param_int(params, "rating", 0) or 0
    if not 0 <= rating <= 5:
        raise SubsonicError(SubsonicError.GENERIC, "Rating must be between 0 and 5")

    for item_type, item_id in _annotation_targets(request, params):
        annotation = scrobble_service.get_or_create_annotation(
            ctx.db, ctx.user.id, item_type, item_id
        )
        annotation.rating = rating
        ctx.db.add(annotation)
    ctx.db.commit()
    return ctx.ok()


@endpoint(router, "scrobble")
def scrobble(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    submission = param_bool(params, "submission", True)

    raw_ids = request.query_params.getlist("id") or (
        [params["id"]] if params.get("id") else []
    )
    raw_times = request.query_params.getlist("time") or (
        [params["time"]] if params.get("time") else []
    )

    if not raw_ids:
        raise SubsonicError(SubsonicError.MISSING_PARAMETER, "Parameter 'id' is missing")

    for index, raw in enumerate(raw_ids):
        track_id = parse_typed_id(raw, TRACK)
        track = ctx.db.get(Track, track_id)
        if track is None:
            continue

        played_at = utcnow()
        if index < len(raw_times):
            try:
                # Subsonic sends epoch milliseconds
                played_at = datetime.fromtimestamp(
                    int(raw_times[index]) / 1000, tz=timezone.utc
                ).replace(tzinfo=None)
            except (TypeError, ValueError, OSError):
                pass

        if submission:
            scrobble_service.record_play(
                ctx.db, ctx.user, track, played_at=played_at, client=ctx.client
            )
        else:
            now_playing.update(ctx.user, track, ctx.client)
            scrobble_service.submit_now_playing(ctx.user, track)

    return ctx.ok()


@endpoint(router, "getLyrics")
def get_lyrics(request: Request, ctx: SubsonicContext = Depends(get_context)):
    params = params_of(request)
    artist = params.get("artist", "")
    title = params.get("title", "")

    stmt = select(Track).where(Track.lyrics.isnot(None))
    if title:
        stmt = stmt.where(Track.title.ilike(f"%{title}%"))
    if artist:
        stmt = stmt.where(Track.artist_name.ilike(f"%{artist}%"))

    track = ctx.db.scalar(stmt.limit(1))
    if track is None or not track.lyrics:
        return ctx.ok({"lyrics": {}})

    return ctx.ok(
        {
            "lyrics": {
                "artist": track.artist_name,
                "title": track.title,
                "value": track.lyrics,
            }
        }
    )
