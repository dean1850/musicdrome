"""AI playlist curation.

The model never invents tracks: it is handed a numbered candidate list drawn
from the user's own library and asked to return the IDs it wants, in order,
with a one-line reason for each. Anything it returns that isn't a real
candidate ID is discarded — a hallucinated ID becomes a missing row, never a
broken playlist.

Candidates are ranked by taste signals (play counts, stars, Last.fm similarity
to the user's top artists) so the slice handed to the model is the most relevant
few hundred tracks rather than an arbitrary sample.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from ...config import settings
from ...db import session_scope, utcnow
from ...models import (
    Annotation,
    ItemType,
    PlayHistory,
    Playlist,
    PlaylistTrack,
    SimilarArtist,
    Track,
    User,
)
from .provider import AIError, get_provider

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the music curator for Musicdrome, a personal music server.

You build playlists from a user's own library. You will be given:
  - a taste profile derived from their listening history
  - a numbered list of candidate tracks from their library

Select tracks that genuinely fit the brief and sequence them like a DJ would:
open strong, build and release energy, avoid putting two tracks by the same
artist back to back unless the brief calls for it, and close deliberately.

Rules:
  - Only use IDs from the candidate list. Never invent a track.
  - Never repeat an ID.
  - Prefer a tight, coherent playlist over padding it to the maximum length.
  - Each reason is one short sentence explaining why that track earned its slot.
"""

PLAYLIST_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Short, evocative playlist name"},
        "description": {"type": "string", "description": "One sentence for the UI"},
        "rationale": {
            "type": "string",
            "description": "Two or three sentences on the overall shape of the selection",
        },
        "tracks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["name", "description", "rationale", "tracks"],
    "additionalProperties": False,
}


# ─── Taste profile ─────────────────────────────────────────────────────────


def build_taste_profile(db: Session, user: User, *, days: int = 180) -> dict:
    """Summarise what this user actually listens to."""
    since = utcnow() - timedelta(days=days)

    top_artists = db.execute(
        select(PlayHistory.artist_name, func.count(PlayHistory.id).label("plays"))
        .where(PlayHistory.user_id == user.id, PlayHistory.played_at >= since)
        .group_by(PlayHistory.artist_name)
        .order_by(func.count(PlayHistory.id).desc())
        .limit(25)
    ).all()

    top_genres = db.execute(
        select(PlayHistory.genre, func.count(PlayHistory.id).label("plays"))
        .where(
            PlayHistory.user_id == user.id,
            PlayHistory.played_at >= since,
            PlayHistory.genre != "",
        )
        .group_by(PlayHistory.genre)
        .order_by(func.count(PlayHistory.id).desc())
        .limit(15)
    ).all()

    starred = db.execute(
        select(Track.artist_name, Track.title)
        .join(
            Annotation,
            and_(
                Annotation.item_id == Track.id,
                Annotation.item_type == ItemType.TRACK.value,
                Annotation.user_id == user.id,
            ),
        )
        .where(Annotation.starred_at.isnot(None))
        .limit(40)
    ).all()

    recent = db.execute(
        select(PlayHistory.artist_name, PlayHistory.title)
        .where(PlayHistory.user_id == user.id)
        .order_by(PlayHistory.played_at.desc())
        .limit(30)
    ).all()

    return {
        "top_artists": [{"artist": a, "plays": p} for a, p in top_artists],
        "top_genres": [{"genre": g, "plays": p} for g, p in top_genres],
        "starred": [f"{a} — {t}" for a, t in starred],
        "recently_played": [f"{a} — {t}" for a, t in recent],
        "total_plays": db.scalar(
            select(func.count(PlayHistory.id)).where(PlayHistory.user_id == user.id)
        )
        or 0,
    }


# ─── Candidate selection ───────────────────────────────────────────────────


def gather_candidates(
    db: Session, user: User, *, limit: int | None = None, seed_genre: str = ""
) -> list[Track]:
    """Rank library tracks by how relevant they are to this user."""
    limit = limit or settings.ai_context_track_limit

    # Artists the user's top artists sound like, so recommendations can reach
    # beyond what they already play heavily.
    top_artist_names = [
        row[0]
        for row in db.execute(
            select(PlayHistory.artist_name, func.count(PlayHistory.id))
            .where(PlayHistory.user_id == user.id)
            .group_by(PlayHistory.artist_name)
            .order_by(func.count(PlayHistory.id).desc())
            .limit(10)
        ).all()
    ]
    neighbour_names = set()
    if top_artist_names:
        neighbour_names = {
            name.lower()
            for name in db.scalars(
                select(SimilarArtist.name)
                .where(SimilarArtist.in_library.is_(True))
                .order_by(SimilarArtist.score.desc())
                .limit(60)
            ).all()
        }

    stmt = (
        select(Track, Annotation)
        .outerjoin(
            Annotation,
            and_(
                Annotation.item_id == Track.id,
                Annotation.item_type == ItemType.TRACK.value,
                Annotation.user_id == user.id,
            ),
        )
        .order_by(func.random())
        .limit(limit * 6)
    )
    if seed_genre:
        stmt = stmt.where(Track.genre.ilike(f"%{seed_genre}%"))

    rows = db.execute(stmt).all()

    def score(track: Track, annotation: Annotation | None) -> float:
        value = 0.0
        if annotation:
            value += min(annotation.play_count, 20) * 1.5
            value += annotation.rating * 4.0
            if annotation.starred_at:
                value += 15.0
        if track.artist_name in top_artist_names:
            value += 10.0
        if track.artist_name.lower() in neighbour_names:
            value += 6.0
        if track.created_at and (utcnow() - track.created_at) < timedelta(days=30):
            value += 3.0
        return value

    ranked = sorted(rows, key=lambda row: score(row[0], row[1]), reverse=True)
    return [row[0] for row in ranked[:limit]]


def _format_candidates(tracks: list[Track]) -> str:
    lines = []
    for track in tracks:
        duration = f"{track.duration // 60}:{track.duration % 60:02d}" if track.duration else "?"
        genre = f" [{track.genre}]" if track.genre else ""
        year = f" ({track.year})" if track.year else ""
        lines.append(
            f"{track.id}: {track.artist_name} — {track.title} "
            f"| {track.album_name}{year}{genre} | {duration}"
        )
    return "\n".join(lines)


# ─── Generation ────────────────────────────────────────────────────────────


def generate_playlist(
    db: Session,
    user: User,
    brief: str,
    *,
    max_tracks: int | None = None,
    seed_genre: str = "",
) -> dict:
    """Ask the model to curate a playlist. Returns the parsed selection."""
    provider = get_provider()
    max_tracks = max_tracks or settings.smart_playlist_max_tracks

    candidates = gather_candidates(db, user, seed_genre=seed_genre)
    if not candidates:
        raise AIError("no tracks in the library to curate from")

    profile = build_taste_profile(db, user)
    prompt = (
        f"Brief: {brief}\n\n"
        f"Target length: up to {max_tracks} tracks.\n\n"
        f"Listener profile:\n{json.dumps(profile, indent=2)}\n\n"
        f"Candidate tracks (id: artist — title | album | genre | length):\n"
        f"{_format_candidates(candidates)}"
    )

    result = provider.complete_json(
        SYSTEM_PROMPT, prompt, schema=PLAYLIST_SCHEMA
    )
    if not isinstance(result, dict):
        raise AIError("model did not return a playlist object")

    valid_ids = {track.id for track in candidates}
    selected: list[dict] = []
    seen: set[int] = set()

    for entry in result.get("tracks", []) or []:
        if not isinstance(entry, dict):
            continue
        try:
            track_id = int(entry.get("id"))
        except (TypeError, ValueError):
            continue
        if track_id not in valid_ids or track_id in seen:
            continue  # hallucinated or duplicated — drop it
        seen.add(track_id)
        selected.append({"id": track_id, "reason": str(entry.get("reason", "")).strip()})
        if len(selected) >= max_tracks:
            break

    if not selected:
        raise AIError("model returned no usable track selections")

    return {
        "name": str(result.get("name") or brief)[:200],
        "description": str(result.get("description") or "")[:1000],
        "rationale": str(result.get("rationale") or "")[:2000],
        "tracks": selected,
        "model": provider.model,
    }


def save_playlist(
    db: Session,
    user: User,
    selection: dict,
    *,
    brief: str,
    playlist: Playlist | None = None,
) -> Playlist:
    """Persist (or refresh) an AI-curated playlist."""
    if playlist is None:
        playlist = Playlist(owner_id=user.id, is_ai=True, public=False)
        db.add(playlist)

    playlist.name = selection["name"]
    playlist.comment = selection["description"]
    playlist.is_ai = True
    playlist.ai_prompt = brief
    playlist.ai_rationale = selection["rationale"]
    playlist.ai_seed = {"model": selection.get("model", ""), "brief": brief}
    playlist.last_generated_at = utcnow()
    db.flush()

    db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == playlist.id).delete(
        synchronize_session=False
    )

    total_duration = 0
    cover: str | None = None
    for position, entry in enumerate(selection["tracks"]):
        track = db.get(Track, entry["id"])
        if track is None:
            continue
        db.add(
            PlaylistTrack(
                playlist_id=playlist.id,
                track_id=track.id,
                position=position,
                note=entry.get("reason") or None,
            )
        )
        total_duration += track.duration
        if cover is None and track.cover_art_path:
            cover = track.cover_art_path

    playlist.song_count = len(selection["tracks"])
    playlist.duration = total_duration
    playlist.cover_art_path = cover
    playlist.updated_at = utcnow()
    db.add(playlist)
    db.commit()
    db.refresh(playlist)
    return playlist


def create_ai_playlist(db: Session, user: User, brief: str, **kwargs) -> Playlist:
    selection = generate_playlist(db, user, brief, **kwargs)
    return save_playlist(db, user, selection, brief=brief)


# ─── Scheduled refresh ─────────────────────────────────────────────────────

DEFAULT_BRIEFS = [
    "A relaxed mix for a slow morning, built from what I already love.",
    "High-energy tracks to work out to.",
    "Deep cuts I own but rarely play — help me rediscover my library.",
    "Focus music: instrumental or low-vocal, nothing distracting.",
]


def refresh_ai_playlists() -> dict[str, int]:
    """Regenerate AI playlists that have gone stale. Called by the scheduler."""
    stats = {"refreshed": 0, "failed": 0}
    if not settings.ai_enabled:
        return stats

    cutoff = utcnow() - timedelta(hours=settings.ai_playlist_refresh_hours)

    with session_scope() as db:
        playlists = db.scalars(
            select(Playlist).where(
                Playlist.is_ai.is_(True),
                (Playlist.last_generated_at.is_(None))
                | (Playlist.last_generated_at < cutoff),
            )
        ).all()

        for playlist in playlists:
            user = db.get(User, playlist.owner_id)
            if user is None or not user.ai_enabled:
                continue
            brief = playlist.ai_prompt or playlist.name
            try:
                selection = generate_playlist(db, user, brief)
                save_playlist(db, user, selection, brief=brief, playlist=playlist)
                stats["refreshed"] += 1
            except AIError as exc:
                log.warning("could not refresh AI playlist %r: %s", playlist.name, exc)
                stats["failed"] += 1
            except Exception:
                log.exception("unexpected failure refreshing AI playlist %r", playlist.name)
                stats["failed"] += 1
                db.rollback()

    if stats["refreshed"]:
        log.info("refreshed %d AI playlists", stats["refreshed"])
    return stats


def seed_ai_playlists(db: Session, user: User) -> int:
    """Create the starter AI playlists for a user with enough history."""
    if not settings.ai_enabled or not user.ai_enabled:
        return 0

    plays = db.scalar(
        select(func.count(PlayHistory.id)).where(PlayHistory.user_id == user.id)
    ) or 0
    if plays < settings.ai_min_plays_for_profile:
        return 0

    created = 0
    for brief in DEFAULT_BRIEFS:
        exists = db.scalar(
            select(Playlist).where(
                Playlist.owner_id == user.id,
                Playlist.is_ai.is_(True),
                Playlist.ai_prompt == brief,
            )
        )
        if exists is not None:
            continue
        try:
            create_ai_playlist(db, user, brief)
            created += 1
        except AIError as exc:
            log.info("skipping seed playlist (%s): %s", brief, exc)
            break  # provider is unavailable — no point trying the rest
    return created
