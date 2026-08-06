"""Playlist mutation helpers shared by the REST API, Subsonic and the importer.

Every writer needs the same two things after touching a track list — the
denormalised rollups recomputed, and generated playlists marked as no longer
generated once a human edits them. Keeping that here means the three callers
cannot drift apart.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import Playlist, PlaylistTrack, Track


def recalculate(db: Session, playlist: Playlist) -> None:
    """Refresh song count, duration and cover art from the current entries."""
    entries = db.scalars(
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
    db.add(playlist)


def replace_tracks(
    db: Session,
    playlist: Playlist,
    track_ids: list[int],
    *,
    notes: dict[int, str] | None = None,
) -> int:
    """Swap the whole track list. Returns how many entries were written.

    Unknown ids are dropped rather than raising: a playlist file naming a track
    that is not in the library should still import the rest of its songs.
    """
    db.query(PlaylistTrack).filter(
        PlaylistTrack.playlist_id == playlist.id
    ).delete(synchronize_session=False)

    position = 0
    for track_id in track_ids:
        if db.get(Track, track_id) is None:
            continue
        db.add(
            PlaylistTrack(
                playlist_id=playlist.id,
                track_id=track_id,
                position=position,
                note=(notes or {}).get(track_id),
            )
        )
        position += 1

    # The sessions here run with autoflush off, so entries have to be pushed
    # before recalculate() can count them — otherwise it reports zero tracks.
    db.flush()
    return position


def detach(playlist: Playlist) -> None:
    """Mark a playlist as hand-edited, so nothing regenerates it underneath.

    A smart playlist loses its rules, an AI one its generator, and an imported
    one stops following its file — but keeps ``import_path`` so the next import
    pass recognises the file instead of creating a duplicate playlist from it.
    """
    playlist.is_smart = False
    playlist.is_ai = False
    playlist.rules = None
    playlist.sync = False
