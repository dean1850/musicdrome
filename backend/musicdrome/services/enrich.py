"""Metadata enrichment.

Fills in what the files themselves do not carry: artist biographies, images,
MusicBrainz IDs and the similarity graph that feeds recommendations and AI
playlist curation. Runs on a schedule and only touches records that are stale or
incomplete, so repeated runs are cheap.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import session_scope, utcnow
from ..models import Album, Artist, SimilarArtist, Track
from .lastfm import lastfm
from .listenbrainz import listenbrainz
from .musicbrainz import musicbrainz

log = logging.getLogger(__name__)

SIMILAR_TTL = timedelta(days=14)
ARTIST_INFO_TTL = timedelta(days=30)


def _download_artist_image(artist: Artist, url: str) -> str | None:
    if not url:
        return None
    target = settings.artists_image_dir / f"{artist.id}.jpg"
    if target.exists():
        return str(target)
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            response = client.get(url)
        if response.status_code != 200 or not response.content:
            return None
        target.write_bytes(response.content)
        return str(target)
    except (httpx.HTTPError, OSError) as exc:
        log.debug("could not fetch artist image for %s: %s", artist.name, exc)
        return None


def enrich_artist(db: Session, artist: Artist, *, force: bool = False) -> bool:
    """Populate biography, image, MBID and genres for one artist."""
    fresh = artist.updated_at and (utcnow() - artist.updated_at) < ARTIST_INFO_TTL
    if fresh and artist.biography and not force:
        return False

    changed = False

    if settings.lastfm_enabled and lastfm.configured:
        info = lastfm.artist_info(artist.name, artist.mbid or "")
        if info:
            if info.get("biography") and not artist.biography:
                artist.biography = info["biography"]
                changed = True
            if info.get("mbid") and not artist.mbid:
                artist.mbid = info["mbid"]
                changed = True
            if info.get("url") and not artist.lastfm_url:
                artist.lastfm_url = info["url"]
                changed = True
            if info.get("listeners"):
                artist.listener_count = info["listeners"]
                changed = True
            if info.get("playcount"):
                artist.global_play_count = info["playcount"]
                changed = True
            if info.get("image_url") and not artist.image_path:
                artist.image_url = info["image_url"]
                path = _download_artist_image(artist, info["image_url"])
                if path:
                    artist.image_path = path
                changed = True

    if settings.musicbrainz_enabled and not artist.mbid:
        mbid = musicbrainz.resolve_artist_mbid(artist.name)
        if mbid:
            artist.mbid = mbid
            changed = True

    if changed:
        artist.updated_at = utcnow()
        db.add(artist)
    return changed


def refresh_similar_artists(db: Session, artist: Artist, *, force: bool = False) -> int:
    """Refresh the cached similarity neighbours for one artist."""
    if not force:
        newest = db.scalar(
            select(SimilarArtist.fetched_at)
            .where(SimilarArtist.artist_id == artist.id)
            .order_by(SimilarArtist.fetched_at.desc())
            .limit(1)
        )
        if newest and (utcnow() - newest) < SIMILAR_TTL:
            return 0

    entries: list[dict] = []
    if settings.lastfm_enabled and lastfm.configured and settings.lastfm_fetch_similar:
        entries.extend(
            {**item, "source": "lastfm"}
            for item in lastfm.similar_artists(artist.name, artist.mbid or "", limit=30)
        )
    if settings.listenbrainz_enabled and artist.mbid:
        entries.extend(
            {**item, "source": "listenbrainz"}
            for item in listenbrainz.similar_artists(artist.mbid, limit=25)
        )

    if not entries:
        return 0

    # Which of these do we already own? Drives "similar artists you have".
    library_names = {
        name.lower()
        for name in db.scalars(select(Artist.name)).all()
    }

    written = 0
    for entry in entries:
        name = (entry.get("name") or "").strip()
        if not name or name.lower() == artist.name.lower():
            continue
        source = entry["source"]
        existing = db.scalar(
            select(SimilarArtist).where(
                SimilarArtist.artist_id == artist.id,
                SimilarArtist.name == name,
                SimilarArtist.source == source,
            )
        )
        if existing is None:
            existing = SimilarArtist(artist_id=artist.id, name=name, source=source)
            db.add(existing)
        existing.mbid = entry.get("mbid") or None
        existing.score = float(entry.get("score", 0) or 0)
        existing.in_library = name.lower() in library_names
        existing.fetched_at = utcnow()
        written += 1

    return written


def enrich_album(db: Session, album: Album) -> bool:
    """Fill album descriptions and MusicBrainz release IDs."""
    changed = False
    if settings.lastfm_enabled and lastfm.configured and not album.description:
        info = lastfm.album_info(album.album_artist or album.artist_name, album.name)
        if info:
            if info.get("description"):
                album.description = info["description"]
                changed = True
            if info.get("mbid") and not album.mbid:
                album.mbid = info["mbid"]
                changed = True
    if changed:
        db.add(album)
    return changed


def enrich_library(limit: int = 50, *, force: bool = False) -> dict[str, int]:
    """Enrich a bounded slice of the library. Safe to call on a timer."""
    stats = {"artists": 0, "albums": 0, "similar": 0}
    if not (settings.lastfm_enabled or settings.musicbrainz_enabled):
        return stats

    with session_scope() as db:
        # Prefer artists the user actually has tracks for, oldest-updated first.
        artists = db.scalars(
            select(Artist)
            .where(Artist.track_count > 0)
            .order_by(Artist.updated_at.asc())
            .limit(limit)
        ).all()

        for artist in artists:
            try:
                if enrich_artist(db, artist, force=force):
                    stats["artists"] += 1
                stats["similar"] += refresh_similar_artists(db, artist, force=force)
            except Exception:
                log.exception("enrichment failed for artist %s", artist.name)
                db.rollback()
        db.commit()

        albums = db.scalars(
            select(Album)
            .where(Album.description.is_(None))
            .order_by(Album.updated_at.asc())
            .limit(limit)
        ).all()
        for album in albums:
            try:
                if enrich_album(db, album):
                    stats["albums"] += 1
            except Exception:
                log.exception("enrichment failed for album %s", album.name)
                db.rollback()
        db.commit()

    log.info(
        "enrichment pass: %d artists, %d albums, %d similarity edges",
        stats["artists"], stats["albums"], stats["similar"],
    )
    return stats


def enrich_track_mbids(limit: int = 100) -> int:
    """Resolve MusicBrainz recording IDs for untagged tracks.

    Only runs when ``MUSICBRAINZ_ENRICH_MODE=all``; the default 'tagged' mode
    trusts whatever the files already carry.
    """
    if not settings.musicbrainz_enabled or settings.musicbrainz_enrich_mode != "all":
        return 0

    updated = 0
    with session_scope() as db:
        tracks = db.scalars(
            select(Track).where(Track.mbid.is_(None)).limit(limit)
        ).all()
        for track in tracks:
            resolved = musicbrainz.resolve_track(
                track.artist_name, track.title, track.album_name
            )
            if resolved.get("recording_mbid"):
                track.mbid = resolved["recording_mbid"]
                if resolved.get("release_mbid") and not track.mb_release_id:
                    track.mb_release_id = resolved["release_mbid"]
                if resolved.get("artist_mbid") and not track.mb_artist_id:
                    track.mb_artist_id = resolved["artist_mbid"]
                db.add(track)
                updated += 1
        db.commit()
    return updated
