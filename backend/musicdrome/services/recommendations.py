"""Recommendation generation.

Three signal sources, merged and de-duplicated against what is already in the
library:

* **Last.fm** — similar artists and similar tracks seeded from the user's own
  top artists.
* **ListenBrainz** — collaborative-filtering recordings and fresh releases.
* **AI** — the model reasons over the taste profile and proposes artists the
  statistical feeds miss.

Anything already in the library is marked ``in_library`` rather than dropped, so
the UI can show "you already own this" instead of silently hiding it.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import session_scope, utcnow
from ..models import Artist, PlayHistory, Recommendation, Track, User, WantedStatus
from ..security import decrypt_secret
from .acquisition import enqueue
from .ai.curator import build_taste_profile
from .ai.provider import AIError, get_provider
from .lastfm import lastfm
from .listenbrainz import listenbrainz

log = logging.getLogger(__name__)

AI_SYSTEM = """You recommend music for a listener based on their listening history.

Suggest artists and tracks they do not already own but are likely to love.
Favour genuine adjacency over obvious mainstream picks: if they listen to a
well-known artist, suggest what that artist's audience actually crosses over to,
not the next most famous name in the genre.

Only recommend real, existing music. Never invent artists or releases.
"""

AI_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "artist": {"type": "string"},
                    "title": {"type": "string", "description": "Track title, or empty for an artist-level pick"},
                    "album": {"type": "string"},
                    "reason": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["artist", "reason", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["recommendations"],
    "additionalProperties": False,
}


# ─── Helpers ───────────────────────────────────────────────────────────────


def _library_index(db: Session) -> tuple[set[str], set[str]]:
    """Lowercased artist names and 'artist|title' pairs already in the library."""
    artists = {name.lower() for (name,) in db.execute(select(Artist.name)).all()}
    tracks = {
        f"{artist.lower()}|{title.lower()}"
        for artist, title in db.execute(
            select(Track.artist_name, Track.title)
        ).all()
    }
    return artists, tracks


def _upsert(
    db: Session,
    user: User,
    *,
    artist: str,
    title: str = "",
    album: str = "",
    source: str,
    score: float,
    reason: str = "",
    seed: str = "",
    mbid: str = "",
    library_artists: set[str] | None = None,
    library_tracks: set[str] | None = None,
) -> Recommendation | None:
    artist = (artist or "").strip()
    if not artist:
        return None

    existing = db.scalar(
        select(Recommendation).where(
            Recommendation.user_id == user.id,
            func.lower(Recommendation.artist_name) == artist.lower(),
            func.lower(Recommendation.title) == title.lower(),
            Recommendation.source == source,
        )
    )
    if existing is not None:
        existing.score = max(existing.score, score)
        if reason and not existing.reason:
            existing.reason = reason
        db.add(existing)
        return existing

    in_library = False
    if library_artists is not None:
        if title and library_tracks is not None:
            in_library = f"{artist.lower()}|{title.lower()}" in library_tracks
        else:
            in_library = artist.lower() in library_artists

    recommendation = Recommendation(
        user_id=user.id,
        item_type="track" if title else "artist",
        artist_name=artist,
        album_name=album,
        title=title,
        mbid=mbid or None,
        source=source,
        score=score,
        reason=reason,
        seed_artist=seed,
        in_library=in_library,
    )
    db.add(recommendation)
    return recommendation


# ─── Sources ───────────────────────────────────────────────────────────────


def from_lastfm(db: Session, user: User, limit: int = 30) -> int:
    if not (settings.lastfm_enabled and lastfm.configured):
        return 0

    library_artists, library_tracks = _library_index(db)
    seeds = db.execute(
        select(PlayHistory.artist_name, func.count(PlayHistory.id))
        .where(PlayHistory.user_id == user.id)
        .group_by(PlayHistory.artist_name)
        .order_by(func.count(PlayHistory.id).desc())
        .limit(8)
    ).all()

    created = 0
    for seed_artist, _plays in seeds:
        for similar in lastfm.similar_artists(seed_artist, limit=10):
            if similar["name"].lower() in library_artists:
                continue
            if _upsert(
                db, user,
                artist=similar["name"],
                source="lastfm",
                score=float(similar.get("score", 0)),
                reason=f"Similar to {seed_artist}, which you play often.",
                seed=seed_artist,
                mbid=similar.get("mbid", ""),
                library_artists=library_artists,
                library_tracks=library_tracks,
            ):
                created += 1
            if created >= limit:
                break
        if created >= limit:
            break

    db.commit()
    return created


def from_listenbrainz(db: Session, user: User, limit: int = 30) -> int:
    if not settings.listenbrainz_enabled or not user.listenbrainz_enabled:
        return 0

    token = decrypt_secret(user.listenbrainz_token) or settings.listenbrainz_token
    if not token:
        return 0

    profile = listenbrainz.validate_token(token)
    username = profile.get("username")
    if not username:
        return 0

    library_artists, library_tracks = _library_index(db)
    created = 0

    for release in listenbrainz.fresh_releases(username)[:limit]:
        if _upsert(
            db, user,
            artist=release["artist"],
            album=release["album"],
            source="listenbrainz",
            score=0.6,
            reason="New release from an artist in your ListenBrainz history.",
            mbid=release.get("mbid", ""),
            library_artists=library_artists,
            library_tracks=library_tracks,
        ):
            created += 1

    db.commit()
    return created


def from_ai(db: Session, user: User, limit: int = 20) -> int:
    if not (settings.ai_enabled and user.ai_enabled):
        return 0

    plays = db.scalar(
        select(func.count(PlayHistory.id)).where(PlayHistory.user_id == user.id)
    ) or 0
    if plays < settings.ai_min_plays_for_profile:
        return 0

    try:
        provider = get_provider()
    except AIError as exc:
        log.debug("AI recommendations unavailable: %s", exc)
        return 0

    library_artists, library_tracks = _library_index(db)
    profile = build_taste_profile(db, user)

    owned_sample = sorted(library_artists)[:400]
    prompt = (
        f"Listener profile:\n{json.dumps(profile, indent=2)}\n\n"
        f"Artists already in their library (do not recommend these):\n"
        f"{', '.join(owned_sample)}\n\n"
        f"Suggest up to {limit} recommendations."
    )

    try:
        result = provider.complete_json(AI_SYSTEM, prompt, schema=AI_SCHEMA)
    except AIError as exc:
        log.info("AI recommendation pass failed: %s", exc)
        return 0

    created = 0
    for entry in (result or {}).get("recommendations", [])[:limit]:
        if not isinstance(entry, dict):
            continue
        artist = str(entry.get("artist", "")).strip()
        if not artist or artist.lower() in library_artists:
            continue
        try:
            confidence = float(entry.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        if _upsert(
            db, user,
            artist=artist,
            title=str(entry.get("title", "")).strip(),
            album=str(entry.get("album", "")).strip(),
            source="ai",
            score=max(0.0, min(1.0, confidence)),
            reason=str(entry.get("reason", ""))[:1000],
            library_artists=library_artists,
            library_tracks=library_tracks,
        ):
            created += 1

    db.commit()
    return created


# ─── Orchestration ─────────────────────────────────────────────────────────


def generate_for_user(db: Session, user: User) -> dict[str, int]:
    stats = {"lastfm": 0, "listenbrainz": 0, "ai": 0}
    sources = settings.recommendation_source_list

    if "lastfm" in sources:
        try:
            stats["lastfm"] = from_lastfm(db, user)
        except Exception:
            log.exception("Last.fm recommendations failed for %s", user.username)
            db.rollback()
    if "listenbrainz" in sources:
        try:
            stats["listenbrainz"] = from_listenbrainz(db, user)
        except Exception:
            log.exception("ListenBrainz recommendations failed for %s", user.username)
            db.rollback()
    if "ai" in sources:
        try:
            stats["ai"] = from_ai(db, user)
        except Exception:
            log.exception("AI recommendations failed for %s", user.username)
            db.rollback()

    return stats


def queue_wanted(db: Session, user: User, limit: int = 10) -> int:
    """Turn the strongest un-owned recommendations into wanted items.

    They land as ``pending`` unless ``AUTO_DOWNLOAD`` is on, so nothing is
    fetched without a decision.
    """
    if not settings.acquisition_enabled:
        return 0

    recommendations = db.scalars(
        select(Recommendation)
        .where(
            Recommendation.user_id == user.id,
            Recommendation.dismissed.is_(False),
            Recommendation.in_library.is_(False),
            Recommendation.score >= settings.acquisition_min_confidence,
        )
        .order_by(Recommendation.score.desc())
        .limit(limit)
    ).all()

    queued = 0
    for recommendation in recommendations:
        provider = "lidarr" if settings.lidarr_enabled else "ytdlp"
        status = (
            WantedStatus.APPROVED.value
            if settings.auto_download
            else WantedStatus.PENDING.value
        )
        enqueue(
            db,
            artist=recommendation.artist_name,
            title=recommendation.title,
            album=recommendation.album_name,
            user_id=user.id,
            source=recommendation.source,
            confidence=recommendation.score,
            reason=recommendation.reason,
            provider=provider,
            status=status,
        )
        queued += 1
    return queued


def refresh_all() -> dict[str, int]:
    """Regenerate recommendations for every active user. Scheduler entry point."""
    totals = {"users": 0, "recommendations": 0, "queued": 0}
    if not settings.recommendations_enabled:
        return totals

    with session_scope() as db:
        users = db.scalars(select(User).where(User.is_active.is_(True))).all()
        for user in users:
            stats = generate_for_user(db, user)
            totals["users"] += 1
            totals["recommendations"] += sum(stats.values())
            try:
                totals["queued"] += queue_wanted(db, user)
            except Exception:
                log.exception("could not queue wanted items for %s", user.username)
                db.rollback()

    if totals["recommendations"]:
        log.info(
            "recommendations: %d generated for %d users (%d queued)",
            totals["recommendations"], totals["users"], totals["queued"],
        )
    return totals


def prune(days: int = 60) -> int:
    """Drop stale, dismissed, or now-owned recommendations."""
    cutoff = utcnow() - timedelta(days=days)
    with session_scope() as db:
        stale = db.scalars(
            select(Recommendation).where(
                (Recommendation.created_at < cutoff)
                | (Recommendation.dismissed.is_(True))
            )
        ).all()
        for recommendation in stale:
            db.delete(recommendation)
        db.commit()
        return len(stale)
