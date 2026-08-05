"""Discovery: recommendations, the wanted queue, and listening analytics."""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..config import settings
from ..db import get_db
from ..models import Recommendation, User, WantedItem, WantedStatus
from ..services import acquisition, recommendations
from ..services.ai import analytics as ai_analytics
from ..services.ai.provider import AIError, provider_status
from .schemas import (
    GenericResponse,
    RecommendationOut,
    SearchDownloadRequest,
    WantedCreateRequest,
    WantedItemOut,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["discovery"])


# ─── Recommendations ───────────────────────────────────────────────────────


@router.get("/recommendations", response_model=list[RecommendationOut])
def list_recommendations(
    source: str = Query("", description="lastfm | listenbrainz | ai"),
    include_owned: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(Recommendation).where(
        Recommendation.user_id == user.id, Recommendation.dismissed.is_(False)
    )
    if source:
        stmt = stmt.where(Recommendation.source == source)
    if not include_owned:
        stmt = stmt.where(Recommendation.in_library.is_(False))

    rows = db.scalars(stmt.order_by(Recommendation.score.desc()).limit(limit)).all()
    return [RecommendationOut.model_validate(row, from_attributes=True) for row in rows]


@router.post("/recommendations/refresh", response_model=GenericResponse)
def refresh_recommendations(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    stats = recommendations.generate_for_user(db, user)
    return GenericResponse(
        message=f"Generated {sum(stats.values())} recommendations", data=stats
    )


@router.post("/recommendations/{recommendation_id}/dismiss", response_model=GenericResponse)
def dismiss_recommendation(
    recommendation_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    recommendation = db.get(Recommendation, recommendation_id)
    if recommendation is None or recommendation.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Recommendation not found"
        )
    recommendation.dismissed = True
    db.add(recommendation)
    db.commit()
    return GenericResponse(message="Dismissed")


@router.post("/recommendations/{recommendation_id}/want", response_model=WantedItemOut)
def want_recommendation(
    recommendation_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    recommendation = db.get(Recommendation, recommendation_id)
    if recommendation is None or recommendation.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Recommendation not found"
        )

    item = acquisition.enqueue(
        db,
        artist=recommendation.artist_name,
        title=recommendation.title,
        album=recommendation.album_name,
        user_id=user.id,
        source=recommendation.source,
        confidence=recommendation.score,
        reason=recommendation.reason,
        provider="lidarr" if settings.lidarr_enabled else "ytdlp",
        status=WantedStatus.APPROVED.value,
    )
    return WantedItemOut.model_validate(item, from_attributes=True)


# ─── Wanted queue ──────────────────────────────────────────────────────────


@router.get("/wanted", response_model=list[WantedItemOut])
def list_wanted(
    status_filter: str = Query("", alias="status"),
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(WantedItem)
    if status_filter:
        stmt = stmt.where(WantedItem.status == status_filter)
    if not user.is_admin:
        stmt = stmt.where(
            (WantedItem.user_id == user.id) | (WantedItem.user_id.is_(None))
        )

    rows = db.scalars(stmt.order_by(WantedItem.created_at.desc()).limit(limit)).all()
    return [WantedItemOut.model_validate(row, from_attributes=True) for row in rows]


@router.post("/wanted", response_model=WantedItemOut, status_code=status.HTTP_201_CREATED)
def create_wanted(
    payload: WantedCreateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not settings.acquisition_enabled and payload.provider == "ytdlp":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Acquisition is disabled (ACQUISITION_ENABLED=false)",
        )

    item = acquisition.enqueue(
        db,
        artist=payload.artist,
        title=payload.title,
        album=payload.album,
        user_id=user.id,
        source="manual",
        confidence=1.0,
        reason=payload.reason,
        provider=payload.provider,
        status=WantedStatus.APPROVED.value,
    )
    return WantedItemOut.model_validate(item, from_attributes=True)


def _load_wanted(db: Session, item_id: int, user: User) -> WantedItem:
    item = db.get(WantedItem, item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    if item.user_id not in (None, user.id) and not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your item")
    return item


@router.post("/wanted/{item_id}/approve", response_model=WantedItemOut)
def approve_wanted(
    item_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    item = acquisition.approve(db, _load_wanted(db, item_id, user))
    return WantedItemOut.model_validate(item, from_attributes=True)


@router.post("/wanted/{item_id}/reject", response_model=WantedItemOut)
def reject_wanted(
    item_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    item = acquisition.reject(db, _load_wanted(db, item_id, user))
    return WantedItemOut.model_validate(item, from_attributes=True)


@router.delete("/wanted/{item_id}", response_model=GenericResponse)
def delete_wanted(
    item_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    item = _load_wanted(db, item_id, user)
    db.delete(item)
    db.commit()
    return GenericResponse(message="Removed from the wanted queue")


@router.post("/wanted/process", response_model=GenericResponse)
def process_wanted_queue(user: User = Depends(get_current_user)):
    """Kick the download worker rather than waiting for the next scheduled run."""
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Administrator access required"
        )

    threading.Thread(
        target=acquisition.process_queue, kwargs={"limit": 5}, daemon=True
    ).start()
    return GenericResponse(message="Download queue is running in the background")


@router.post("/acquisition/search")
def search_downloadable(
    payload: SearchDownloadRequest, user: User = Depends(get_current_user)
):
    """Preview what yt-dlp would fetch for a query, without downloading."""
    if not settings.acquisition_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Acquisition is disabled"
        )
    try:
        candidates = acquisition.search(
            payload.query, artist=payload.artist, title=payload.title
        )
    except acquisition.AcquisitionError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    return [
        {
            "url": candidate.url,
            "title": candidate.title,
            "uploader": candidate.uploader,
            "duration": candidate.duration,
            "score": round(candidate.score, 3),
        }
        for candidate in candidates[:10]
    ]


# ─── Analytics ─────────────────────────────────────────────────────────────


@router.get("/analytics/stats")
def analytics_stats(
    period: str = Query("month", pattern="^(week|month|quarter|year|all)$"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return {
        **ai_analytics.compute_stats(db, user, period),
        "genre_mix": ai_analytics.genre_mix(db, user, period),
        "comparison": ai_analytics.compare_periods(db, user, period),
    }


@router.get("/analytics/insights")
def analytics_insights(
    period: str = Query("month", pattern="^(week|month|quarter|year|all)$"),
    refresh: bool = Query(False),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user.ai_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="AI features are turned off for your account",
        )

    report = ai_analytics.get_or_create_report(db, user, period, force=refresh)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "No insights available yet — either the AI provider is not "
                "configured or there is not enough listening history."
            ),
        )

    return {
        "id": report.id,
        "period": report.period,
        "summary": report.summary,
        "model": report.model,
        "created_at": report.created_at.isoformat(),
        "payload": report.payload,
    }


@router.get("/ai/status")
def ai_status(user: User = Depends(get_current_user)):
    return provider_status()


@router.post("/ai/test", response_model=GenericResponse)
def ai_test(user: User = Depends(get_current_user)):
    """Round-trip the configured provider so misconfiguration surfaces early."""
    from ..services.ai.provider import get_provider

    try:
        provider = get_provider()
        reply = provider.complete(
            "You are a connectivity check. Reply with exactly: OK",
            "Reply with exactly: OK",
            max_tokens=16,
        )
    except AIError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))

    return GenericResponse(
        message=f"{provider.name} responded",
        data={"model": provider.model, "reply": reply[:200]},
    )
