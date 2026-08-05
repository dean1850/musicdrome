"""Play recording and scrobbling.

A play does three things:

1. appends to :class:`PlayHistory` — the local record analytics are built from;
2. bumps the per-user :class:`Annotation` counters that Subsonic exposes;
3. enqueues one :class:`ScrobbleQueue` row per enabled external service.

The queue exists so a Last.fm outage does not lose plays: rows are retried with
exponential backoff by the scheduler and only marked ``sent`` on success.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import session_scope, utcnow
from ..models import (
    Annotation,
    ItemType,
    PlayHistory,
    ScrobbleQueue,
    ScrobbleStatus,
    Track,
    User,
)
from ..security import decrypt_secret
from .lastfm import LastFmError, lastfm
from .listenbrainz import ListenBrainzError, listenbrainz

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 6
# Backoff in minutes per attempt; the last value repeats.
BACKOFF_MINUTES = [1, 5, 15, 60, 240, 720]


# ─── Annotations ───────────────────────────────────────────────────────────


def get_or_create_annotation(
    db: Session, user_id: int, item_type: str, item_id: int
) -> Annotation:
    annotation = db.scalar(
        select(Annotation).where(
            Annotation.user_id == user_id,
            Annotation.item_type == item_type,
            Annotation.item_id == item_id,
        )
    )
    if annotation is None:
        annotation = Annotation(user_id=user_id, item_type=item_type, item_id=item_id)
        db.add(annotation)
        db.flush()
    return annotation


def _bump_play_counts(db: Session, user: User, track: Track, played_at: datetime) -> None:
    for item_type, item_id in (
        (ItemType.TRACK.value, track.id),
        (ItemType.ALBUM.value, track.album_id),
        (ItemType.ARTIST.value, track.artist_id),
    ):
        if not item_id:
            continue
        annotation = get_or_create_annotation(db, user.id, item_type, item_id)
        annotation.play_count += 1
        annotation.play_date = played_at


# ─── Recording a play ──────────────────────────────────────────────────────


def record_play(
    db: Session,
    user: User,
    track: Track,
    *,
    played_at: datetime | None = None,
    client: str = "",
    submit: bool = True,
) -> PlayHistory:
    """Record a completed play and queue it for external submission."""
    played_at = played_at or utcnow()

    history = PlayHistory(
        user_id=user.id,
        track_id=track.id,
        title=track.title,
        artist_name=track.artist_name,
        album_name=track.album_name,
        genre=track.genre,
        duration=track.duration,
        played_at=played_at,
        client=client,
        source="stream",
    )
    db.add(history)
    _bump_play_counts(db, user, track, played_at)

    if submit:
        enqueue_scrobbles(db, user, track, played_at)

    db.commit()
    return history


def enqueue_scrobbles(
    db: Session, user: User, track: Track, played_at: datetime
) -> list[ScrobbleQueue]:
    """Create one queue row per external service this user has enabled."""
    services: list[str] = []
    if (
        settings.lastfm_enabled
        and settings.lastfm_scrobble_enabled
        and user.lastfm_enabled
        and user.lastfm_session_key
    ):
        services.append("lastfm")
    if (
        settings.listenbrainz_enabled
        and settings.listenbrainz_scrobble_enabled
        and user.listenbrainz_enabled
        and (user.listenbrainz_token or settings.listenbrainz_token)
    ):
        services.append("listenbrainz")

    rows: list[ScrobbleQueue] = []
    for service in services:
        row = ScrobbleQueue(
            user_id=user.id,
            track_id=track.id,
            service=service,
            title=track.title,
            artist_name=track.artist_name,
            album_name=track.album_name,
            album_artist=track.album_artist,
            track_number=track.track_number,
            duration=track.duration,
            mbid=track.mbid,
            played_at=played_at,
            status=ScrobbleStatus.PENDING.value,
            next_attempt_at=utcnow(),
        )
        db.add(row)
        rows.append(row)
    return rows


def submit_now_playing(user: User, track: Track) -> None:
    """Best-effort 'now playing' ping. Never raises into the request path."""
    if settings.lastfm_enabled and user.lastfm_enabled and user.lastfm_session_key:
        session_key = decrypt_secret(user.lastfm_session_key)
        if session_key:
            try:
                lastfm.update_now_playing(
                    session_key,
                    track.artist_name,
                    track.title,
                    album=track.album_name,
                    album_artist=track.album_artist,
                    duration=track.duration,
                    track_number=track.track_number,
                    mbid=track.mbid or "",
                )
            except LastFmError as exc:
                log.debug("last.fm now-playing failed for %s: %s", user.username, exc)

    if settings.listenbrainz_enabled and user.listenbrainz_enabled:
        token = decrypt_secret(user.listenbrainz_token) or settings.listenbrainz_token
        if token:
            try:
                listenbrainz.submit_playing_now(
                    token,
                    track.artist_name,
                    track.title,
                    album=track.album_name,
                    duration=track.duration,
                    mbid=track.mbid or "",
                )
            except ListenBrainzError as exc:
                log.debug(
                    "listenbrainz now-playing failed for %s: %s", user.username, exc
                )


# ─── Queue processing ──────────────────────────────────────────────────────


def _send_one(db: Session, row: ScrobbleQueue) -> bool:
    user = db.get(User, row.user_id)
    if user is None:
        row.status = ScrobbleStatus.SKIPPED.value
        row.last_error = "user no longer exists"
        return False

    if row.service == "lastfm":
        session_key = decrypt_secret(user.lastfm_session_key)
        if not session_key:
            row.status = ScrobbleStatus.SKIPPED.value
            row.last_error = "no Last.fm session key"
            return False
        lastfm.scrobble(
            session_key,
            row.artist_name,
            row.title,
            row.played_at,
            album=row.album_name,
            album_artist=row.album_artist,
            duration=row.duration,
            track_number=row.track_number,
            mbid=row.mbid or "",
        )
        return True

    if row.service == "listenbrainz":
        token = decrypt_secret(user.listenbrainz_token) or settings.listenbrainz_token
        if not token:
            row.status = ScrobbleStatus.SKIPPED.value
            row.last_error = "no ListenBrainz token"
            return False
        listenbrainz.submit_listen(
            token,
            row.artist_name,
            row.title,
            row.played_at,
            album=row.album_name,
            duration=row.duration,
            track_number=row.track_number,
            mbid=row.mbid or "",
        )
        return True

    row.status = ScrobbleStatus.SKIPPED.value
    row.last_error = f"unknown service {row.service}"
    return False


def process_queue(limit: int = 100) -> dict[str, int]:
    """Drain due scrobbles. Called by the scheduler every minute."""
    stats = {"sent": 0, "failed": 0, "skipped": 0}
    now = utcnow()

    with session_scope() as db:
        rows = db.scalars(
            select(ScrobbleQueue)
            .where(
                ScrobbleQueue.status == ScrobbleStatus.PENDING.value,
                (ScrobbleQueue.next_attempt_at.is_(None))
                | (ScrobbleQueue.next_attempt_at <= now),
            )
            .order_by(ScrobbleQueue.played_at.asc())
            .limit(limit)
        ).all()

        for row in rows:
            row.attempts += 1
            try:
                sent = _send_one(db, row)
            except (LastFmError, ListenBrainzError) as exc:
                sent = False
                row.last_error = str(exc)[:500]
            except Exception as exc:
                sent = False
                row.last_error = f"unexpected error: {exc}"[:500]
                log.exception("scrobble submission crashed")

            if sent:
                row.status = ScrobbleStatus.SENT.value
                row.last_error = None
                stats["sent"] += 1
            elif row.status == ScrobbleStatus.SKIPPED.value:
                stats["skipped"] += 1
            elif row.attempts >= MAX_ATTEMPTS:
                row.status = ScrobbleStatus.FAILED.value
                stats["failed"] += 1
                log.warning(
                    "giving up on %s scrobble for %s — %s",
                    row.service, row.artist_name, row.last_error,
                )
            else:
                index = min(row.attempts - 1, len(BACKOFF_MINUTES) - 1)
                row.next_attempt_at = now + timedelta(minutes=BACKOFF_MINUTES[index])
                stats["failed"] += 1

            db.add(row)
        db.commit()

    if stats["sent"]:
        log.info("scrobbles sent: %d", stats["sent"])
    return stats


def prune_queue(days: int = 30) -> int:
    """Drop long-settled queue rows so the table does not grow forever."""
    cutoff = utcnow() - timedelta(days=days)
    with session_scope() as db:
        rows = db.scalars(
            select(ScrobbleQueue).where(
                ScrobbleQueue.status.in_(
                    [
                        ScrobbleStatus.SENT.value,
                        ScrobbleStatus.SKIPPED.value,
                        ScrobbleStatus.FAILED.value,
                    ]
                ),
                ScrobbleQueue.created_at < cutoff,
            )
        ).all()
        for row in rows:
            db.delete(row)
        db.commit()
        return len(rows)


# ─── Account linking ───────────────────────────────────────────────────────


def link_lastfm(db: Session, user: User, username: str, password: str) -> str:
    """Exchange Last.fm credentials for a session key stored encrypted."""
    from ..security import encrypt_secret

    session = lastfm.get_mobile_session(username, password)
    if not session.get("key"):
        raise LastFmError(4, "Last.fm did not return a session key")

    user.lastfm_username = session.get("name", username)
    user.lastfm_session_key = encrypt_secret(session["key"])
    user.lastfm_enabled = True
    db.add(user)
    db.commit()
    return user.lastfm_username


def link_listenbrainz(db: Session, user: User, token: str) -> str:
    from ..security import encrypt_secret

    result = listenbrainz.validate_token(token)
    if not result.get("valid"):
        raise ListenBrainzError("token rejected by ListenBrainz")

    user.listenbrainz_token = encrypt_secret(token)
    user.listenbrainz_enabled = True
    db.add(user)
    db.commit()
    return result.get("username", "")
