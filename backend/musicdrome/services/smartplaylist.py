"""Rule-based smart playlists.

The rule document is a JSON tree modelled on Navidrome's ``.nsp`` criteria
format, so existing rule sets port across with little change::

    {
      "all": [
        {"is":         {"genre": "Jazz"}},
        {"gt":         {"playCount": 3}},
        {"inTheLast":  {"lastPlayed": 90}}
      ],
      "sort":  "playCount",
      "order": "desc",
      "limit": 50
    }

``all`` / ``any`` / ``not`` nest arbitrarily. Fields that live in the per-user
annotation table (play count, rating, stars) are resolved against the playlist
owner, so the same rule set produces different results for different users.
"""

from __future__ import annotations

import logging
import random
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from ..config import settings
from ..db import session_scope, utcnow
from ..models import Annotation, ItemType, Playlist, PlaylistTrack, Track, User

log = logging.getLogger(__name__)


class RuleError(ValueError):
    """Raised when a rule document cannot be interpreted."""


# ─── Field registry ────────────────────────────────────────────────────────
# ``annotation`` fields are resolved through the outer-joined Annotation row.

TRACK_FIELDS: dict[str, Any] = {
    "title": Track.title,
    "album": Track.album_name,
    "artist": Track.artist_name,
    "albumartist": Track.album_artist,
    "genre": Track.genre,
    "year": Track.year,
    "tracknumber": Track.track_number,
    "discnumber": Track.disc_number,
    "bitrate": Track.bitrate,
    "duration": Track.duration,
    "size": Track.size,
    "comment": Track.comment,
    "bpm": Track.bpm,
    "filepath": Track.path,
    "filetype": Track.suffix,
    "dateadded": Track.created_at,
    "datemodified": Track.updated_at,
}

ANNOTATION_FIELDS: dict[str, Any] = {
    "playcount": Annotation.play_count,
    "lastplayed": Annotation.play_date,
    "rating": Annotation.rating,
    "starred": Annotation.starred_at,
    "loved": Annotation.starred_at,
}

DATE_FIELDS = {"dateadded", "datemodified", "lastplayed"}

SORT_FIELDS: dict[str, Any] = {
    **TRACK_FIELDS,
    **ANNOTATION_FIELDS,
    "random": None,
}


def _resolve_field(name: str) -> tuple[Any, str]:
    key = str(name).strip().lower().replace("_", "")
    if key in TRACK_FIELDS:
        return TRACK_FIELDS[key], key
    if key in ANNOTATION_FIELDS:
        return ANNOTATION_FIELDS[key], key
    raise RuleError(f"unknown field: {name}")


def _single_pair(payload: Any, operator: str) -> tuple[str, Any]:
    if not isinstance(payload, dict) or len(payload) != 1:
        raise RuleError(f"operator '{operator}' expects exactly one field/value pair")
    return next(iter(payload.items()))


# ─── Operators ─────────────────────────────────────────────────────────────


def _build_condition(operator: str, payload: Any) -> ColumnElement[bool]:
    operator = operator.strip()

    if operator in {"all", "any", "not"}:
        if operator == "not":
            clauses = payload if isinstance(payload, list) else [payload]
            return not_(and_(*[_build_group(clause) for clause in clauses]))
        if not isinstance(payload, list):
            raise RuleError(f"'{operator}' expects a list of conditions")
        clauses = [_build_group(clause) for clause in payload]
        if not clauses:
            return func.coalesce(True, True) == True  # noqa: E712 — always-true
        return and_(*clauses) if operator == "all" else or_(*clauses)

    field_name, value = _single_pair(payload, operator)
    column, key = _resolve_field(field_name)

    # Star/love are stored as a nullable timestamp, so treat them as booleans.
    if key in {"starred", "loved"} and operator in {"is", "isnot"}:
        truthy = bool(value) if not isinstance(value, str) else value.lower() in {"true", "1", "yes"}
        if operator == "isnot":
            truthy = not truthy
        return column.isnot(None) if truthy else column.is_(None)

    match operator:
        case "is" | "eq":
            if isinstance(value, str):
                return func.lower(column) == value.lower()
            return column == value
        case "isNot" | "isnot" | "ne":
            if isinstance(value, str):
                return or_(func.lower(column) != value.lower(), column.is_(None))
            return or_(column != value, column.is_(None))
        case "gt":
            return column > value
        case "gte" | "ge":
            return column >= value
        case "lt":
            return column < value
        case "lte" | "le":
            return column <= value
        case "contains":
            return column.ilike(f"%{value}%")
        case "notContains" | "notcontains":
            return or_(~column.ilike(f"%{value}%"), column.is_(None))
        case "startsWith" | "startswith":
            return column.ilike(f"{value}%")
        case "endsWith" | "endswith":
            return column.ilike(f"%{value}")
        case "inTheRange" | "intherange":
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise RuleError("'inTheRange' expects [min, max]")
            return column.between(value[0], value[1])
        case "before":
            return and_(column.isnot(None), column < _as_datetime(value))
        case "after":
            return and_(column.isnot(None), column > _as_datetime(value))
        case "inTheLast" | "inthelast":
            if key not in DATE_FIELDS:
                raise RuleError(f"'inTheLast' needs a date field, got {field_name}")
            cutoff = utcnow() - timedelta(days=float(value))
            return and_(column.isnot(None), column >= cutoff)
        case "notInTheLast" | "notinthelast":
            if key not in DATE_FIELDS:
                raise RuleError(f"'notInTheLast' needs a date field, got {field_name}")
            cutoff = utcnow() - timedelta(days=float(value))
            return or_(column.is_(None), column < cutoff)
        case "isNull" | "isnull":
            return column.is_(None)
        case "isNotNull" | "isnotnull":
            return column.isnot(None)
        case _:
            raise RuleError(f"unknown operator: {operator}")


def _as_datetime(value: Any):
    from datetime import datetime

    if isinstance(value, datetime):
        return value
    text = str(value)
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise RuleError(f"cannot parse date: {value}")


def _build_group(node: Any) -> ColumnElement[bool]:
    if not isinstance(node, dict):
        raise RuleError(f"expected an object, got {type(node).__name__}")
    clauses = [
        _build_condition(operator, payload)
        for operator, payload in node.items()
        if operator not in {"sort", "order", "limit", "offset"}
    ]
    if not clauses:
        raise RuleError("condition group is empty")
    return and_(*clauses)


# ─── Evaluation ────────────────────────────────────────────────────────────


def evaluate_rules(db: Session, rules: dict, user: User, *, limit: int | None = None) -> list[Track]:
    """Run a rule document and return the matching tracks in order."""
    if not isinstance(rules, dict):
        raise RuleError("rules must be an object")

    conditions = {k: v for k, v in rules.items() if k not in {"sort", "order", "limit", "offset"}}
    max_tracks = limit or int(rules.get("limit") or settings.smart_playlist_max_tracks)
    offset = int(rules.get("offset") or 0)

    stmt = select(Track).outerjoin(
        Annotation,
        and_(
            Annotation.item_id == Track.id,
            Annotation.item_type == ItemType.TRACK.value,
            Annotation.user_id == user.id,
        ),
    )

    if conditions:
        stmt = stmt.where(_build_group(conditions))

    sort_key = str(rules.get("sort") or "").lower().replace("_", "")
    order = str(rules.get("order") or "asc").lower()

    if sort_key == "random":
        stmt = stmt.order_by(func.random())
    elif sort_key:
        if sort_key not in SORT_FIELDS:
            raise RuleError(f"cannot sort by unknown field: {rules.get('sort')}")
        column = SORT_FIELDS[sort_key]
        stmt = stmt.order_by(column.desc() if order in {"desc", "descending"} else column.asc())
    else:
        stmt = stmt.order_by(Track.artist_name.asc(), Track.album_name.asc(),
                             Track.disc_number.asc(), Track.track_number.asc())

    stmt = stmt.offset(offset).limit(max_tracks)
    return list(db.scalars(stmt).all())


def validate_rules(rules: dict) -> None:
    """Raise :class:`RuleError` if a rule document will not evaluate."""
    conditions = {k: v for k, v in rules.items() if k not in {"sort", "order", "limit", "offset"}}
    if conditions:
        _build_group(conditions)
    sort_key = str(rules.get("sort") or "").lower().replace("_", "")
    if sort_key and sort_key not in SORT_FIELDS:
        raise RuleError(f"cannot sort by unknown field: {rules.get('sort')}")


# ─── Materialisation ───────────────────────────────────────────────────────


def refresh_playlist(db: Session, playlist: Playlist) -> int:
    """Recompute a smart playlist's contents. Returns the new track count."""
    if not playlist.is_smart or not playlist.rules:
        return playlist.song_count

    owner = db.get(User, playlist.owner_id)
    if owner is None:
        return 0

    try:
        tracks = evaluate_rules(db, playlist.rules, owner)
    except RuleError as exc:
        log.warning("smart playlist %r has invalid rules: %s", playlist.name, exc)
        return playlist.song_count

    db.query(PlaylistTrack).filter(PlaylistTrack.playlist_id == playlist.id).delete(
        synchronize_session=False
    )
    for position, track in enumerate(tracks):
        db.add(
            PlaylistTrack(playlist_id=playlist.id, track_id=track.id, position=position)
        )

    playlist.song_count = len(tracks)
    playlist.duration = sum(track.duration for track in tracks)
    playlist.last_generated_at = utcnow()
    playlist.updated_at = utcnow()
    if tracks and not playlist.cover_art_path:
        playlist.cover_art_path = tracks[0].cover_art_path
    db.add(playlist)
    return len(tracks)


def refresh_all(user_id: int | None = None) -> dict[str, int]:
    """Refresh every smart playlist (optionally just one user's)."""
    stats = {"playlists": 0, "tracks": 0}
    if not settings.smart_playlist_enabled:
        return stats

    with session_scope() as db:
        stmt = select(Playlist).where(Playlist.is_smart.is_(True))
        if user_id is not None:
            stmt = stmt.where(Playlist.owner_id == user_id)
        for playlist in db.scalars(stmt).all():
            try:
                count = refresh_playlist(db, playlist)
                stats["playlists"] += 1
                stats["tracks"] += count
            except Exception:
                log.exception("failed to refresh smart playlist %s", playlist.name)
                db.rollback()
        db.commit()

    if stats["playlists"]:
        log.info(
            "refreshed %d smart playlists (%d tracks)",
            stats["playlists"], stats["tracks"],
        )
    return stats


# ─── Starter set ───────────────────────────────────────────────────────────

DEFAULT_PLAYLISTS: list[dict[str, Any]] = [
    {
        "name": "Recently Added",
        "comment": "Everything that landed in the library in the last 30 days.",
        "rules": {
            "all": [{"inTheLast": {"dateAdded": 30}}],
            "sort": "dateAdded",
            "order": "desc",
            "limit": 100,
        },
    },
    {
        "name": "Most Played",
        "comment": "Your heaviest rotation.",
        "rules": {
            "all": [{"gt": {"playCount": 0}}],
            "sort": "playCount",
            "order": "desc",
            "limit": 100,
        },
    },
    {
        "name": "Forgotten Gems",
        "comment": "Tracks you used to play a lot but have not touched in six months.",
        "rules": {
            "all": [
                {"gt": {"playCount": 3}},
                {"notInTheLast": {"lastPlayed": 180}},
            ],
            "sort": "playCount",
            "order": "desc",
            "limit": 100,
        },
    },
    {
        "name": "Never Played",
        "comment": "The corners of your library you have not explored yet.",
        "rules": {
            "any": [{"is": {"playCount": 0}}, {"isNull": {"playCount": None}}],
            "sort": "random",
            "limit": 100,
        },
    },
    {
        "name": "Favourites",
        "comment": "Everything you have starred.",
        "rules": {
            "all": [{"is": {"starred": True}}],
            "sort": "lastPlayed",
            "order": "desc",
            "limit": 200,
        },
    },
    {
        "name": "Top Rated",
        "comment": "Four stars and up.",
        "rules": {
            "all": [{"gte": {"rating": 4}}],
            "sort": "rating",
            "order": "desc",
            "limit": 100,
        },
    },
]


def seed_default_playlists(db: Session, user: User) -> int:
    """Create the starter smart playlists for a new user."""
    if not settings.smart_playlist_seed_defaults:
        return 0

    created = 0
    for spec in DEFAULT_PLAYLISTS:
        exists = db.scalar(
            select(Playlist).where(
                Playlist.owner_id == user.id, Playlist.name == spec["name"]
            )
        )
        if exists is not None:
            continue
        playlist = Playlist(
            name=spec["name"],
            comment=spec["comment"],
            owner_id=user.id,
            public=False,
            is_smart=True,
            rules=spec["rules"],
        )
        db.add(playlist)
        db.flush()
        # Materialise immediately. Without this the starter playlists would read
        # as empty until the first scheduled refresh, which is up to an hour
        # after the account is created.
        refresh_playlist(db, playlist)
        created += 1

    db.commit()
    return created


def shuffle_tracks(tracks: list[Track], seed: int | None = None) -> list[Track]:
    """Deterministic shuffle used by the random-playlist endpoints."""
    ordered = list(tracks)
    random.Random(seed).shuffle(ordered)
    return ordered
