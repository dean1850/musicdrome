"""Podcast subscriptions.

Feeds are parsed with ``feedparser``, episodes are tracked by GUID, and audio is
downloaded on demand (or automatically when ``PODCAST_AUTO_DOWNLOAD=true``).
Retention is enforced per channel by ``PODCAST_KEEP_EPISODES``, oldest first.

Subsonic's podcast verbs map onto this module directly, so a Subsonic client and
the web UI see the same subscriptions.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

import feedparser
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import session_scope, utcnow
from ..models import PodcastChannel, PodcastEpisode, User

log = logging.getLogger(__name__)

_download_slots = threading.Semaphore(settings.podcast_max_concurrent_downloads)

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


def _safe(name: str, fallback: str = "episode") -> str:
    cleaned = _SAFE_NAME.sub("", (name or "").strip()).strip(". ")
    return (cleaned or fallback)[:120]


def _parse_date(entry) -> datetime | None:
    parsed = getattr(entry, "published_parsed", None) or getattr(
        entry, "updated_parsed", None
    )
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6])
    except (TypeError, ValueError):
        return None


def _parse_duration(value) -> int:
    """iTunes durations come as seconds, MM:SS or HH:MM:SS."""
    if value is None:
        return 0
    text = str(value).strip()
    if not text:
        return 0
    if ":" not in text:
        try:
            return int(float(text))
        except ValueError:
            return 0
    total = 0
    for part in text.split(":"):
        try:
            total = total * 60 + int(part)
        except ValueError:
            return 0
    return total


def _audio_enclosure(entry) -> dict | None:
    for link in getattr(entry, "links", []) or []:
        if link.get("rel") == "enclosure" and "audio" in (link.get("type") or ""):
            return link
    for enclosure in getattr(entry, "enclosures", []) or []:
        if "audio" in (enclosure.get("type") or "") or enclosure.get("href"):
            return enclosure
    return None


# ─── Channels ──────────────────────────────────────────────────────────────


def add_channel(db: Session, url: str, user: User | None = None) -> PodcastChannel:
    """Subscribe to a feed and pull its episode list immediately."""
    url = url.strip()
    if not url:
        raise ValueError("feed URL is required")

    existing = db.scalar(select(PodcastChannel).where(PodcastChannel.url == url))
    if existing is not None:
        return existing

    channel = PodcastChannel(
        url=url,
        status="new",
        created_by=user.id if user else None,
        auto_download=settings.podcast_auto_download,
    )
    db.add(channel)
    db.commit()
    db.refresh(channel)

    try:
        refresh_channel(db, channel)
    except Exception as exc:
        log.warning("initial fetch failed for %s: %s", url, exc)
    return channel


def refresh_channel(db: Session, channel: PodcastChannel) -> int:
    """Re-read a feed. Returns the number of new episodes discovered."""
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            response = client.get(
                channel.url, headers={"User-Agent": "Musicdrome/1.0 podcast client"}
            )
        response.raise_for_status()
        feed = feedparser.parse(response.content)
    except (httpx.HTTPError, ValueError) as exc:
        channel.status = "error"
        channel.error_message = str(exc)[:500]
        db.add(channel)
        db.commit()
        raise

    if getattr(feed, "bozo", False) and not getattr(feed, "entries", None):
        channel.status = "error"
        channel.error_message = str(getattr(feed, "bozo_exception", "unparseable feed"))[:500]
        db.add(channel)
        db.commit()
        raise ValueError(channel.error_message)

    info = feed.feed
    channel.title = info.get("title", channel.title or channel.url)
    channel.description = info.get("subtitle") or info.get("description") or ""
    channel.author = info.get("author") or (info.get("publisher_detail", {}) or {}).get("name", "")
    channel.link = info.get("link", "")
    channel.language = info.get("language", "")
    channel.categories = [t.get("term", "") for t in (info.get("tags") or []) if t.get("term")]

    image = info.get("image") or {}
    if isinstance(image, dict) and image.get("href"):
        channel.image_url = image["href"]

    channel.status = "completed"
    channel.error_message = None
    channel.last_fetched_at = utcnow()

    known = {
        guid
        for (guid,) in db.execute(
            select(PodcastEpisode.guid).where(PodcastEpisode.channel_id == channel.id)
        ).all()
    }

    new_count = 0
    for entry in getattr(feed, "entries", []) or []:
        enclosure = _audio_enclosure(entry)
        if not enclosure:
            continue
        stream_url = enclosure.get("href") or enclosure.get("url") or ""
        if not stream_url:
            continue

        guid = getattr(entry, "id", None) or stream_url
        if guid in known:
            continue

        suffix = Path(stream_url.split("?")[0]).suffix.lstrip(".").lower() or "mp3"
        episode = PodcastEpisode(
            channel_id=channel.id,
            guid=guid,
            title=getattr(entry, "title", "Untitled episode"),
            description=getattr(entry, "summary", "") or "",
            publish_date=_parse_date(entry),
            duration=_parse_duration(entry.get("itunes_duration")),
            size=int(enclosure.get("length") or 0),
            suffix=suffix,
            content_type=enclosure.get("type") or "audio/mpeg",
            stream_url=stream_url,
            status="new",
        )
        db.add(episode)
        new_count += 1
        known.add(guid)

    db.add(channel)
    db.commit()

    if channel.auto_download and new_count:
        for episode in db.scalars(
            select(PodcastEpisode)
            .where(
                PodcastEpisode.channel_id == channel.id,
                PodcastEpisode.status == "new",
            )
            .order_by(PodcastEpisode.publish_date.desc())
            .limit(new_count)
        ).all():
            try:
                download_episode(db, episode)
            except Exception:
                log.exception("auto-download failed for %s", episode.title)

    if new_count:
        log.info("%s: %d new episodes", channel.title, new_count)
    enforce_retention(db, channel)
    return new_count


def delete_channel(db: Session, channel: PodcastChannel) -> None:
    """Unsubscribe and remove any downloaded audio."""
    for episode in list(channel.episodes):
        _remove_file(episode)
    db.delete(channel)
    db.commit()


# ─── Episodes ──────────────────────────────────────────────────────────────


def _episode_path(channel: PodcastChannel, episode: PodcastEpisode) -> Path:
    folder = settings.podcast_dir / _safe(channel.title or f"channel-{channel.id}", "podcast")
    folder.mkdir(parents=True, exist_ok=True)
    stamp = episode.publish_date.strftime("%Y-%m-%d") if episode.publish_date else "undated"
    return folder / f"{stamp} - {_safe(episode.title)}.{episode.suffix or 'mp3'}"


def _remove_file(episode: PodcastEpisode) -> None:
    if not episode.path:
        return
    try:
        Path(episode.path).unlink(missing_ok=True)
    except OSError:
        pass
    episode.path = None


def download_episode(db: Session, episode: PodcastEpisode) -> PodcastEpisode:
    """Fetch an episode to disk. Idempotent."""
    if episode.status == "completed" and episode.path and Path(episode.path).exists():
        return episode

    channel = db.get(PodcastChannel, episode.channel_id)
    if channel is None:
        raise ValueError("episode has no channel")

    episode.status = "downloading"
    episode.error_message = None
    db.add(episode)
    db.commit()

    target = _episode_path(channel, episode)
    partial = target.with_suffix(target.suffix + ".part")

    acquired = _download_slots.acquire(timeout=300)
    if not acquired:
        episode.status = "error"
        episode.error_message = "timed out waiting for a download slot"
        db.add(episode)
        db.commit()
        raise RuntimeError(episode.error_message)

    try:
        with httpx.Client(timeout=120.0, follow_redirects=True) as client:
            with client.stream("GET", episode.stream_url) as response:
                response.raise_for_status()
                with partial.open("wb") as handle:
                    for chunk in response.iter_bytes(chunk_size=64 * 1024):
                        handle.write(chunk)
        partial.replace(target)

        episode.path = str(target)
        episode.size = target.stat().st_size
        episode.status = "completed"
        log.info("downloaded episode: %s", episode.title)
    except (httpx.HTTPError, OSError) as exc:
        partial.unlink(missing_ok=True)
        episode.status = "error"
        episode.error_message = str(exc)[:500]
        log.warning("episode download failed (%s): %s", episode.title, exc)
        raise
    finally:
        _download_slots.release()
        db.add(episode)
        db.commit()

    enforce_retention(db, channel)
    return episode


def delete_episode(db: Session, episode: PodcastEpisode) -> None:
    """Remove the downloaded file but keep the episode listed."""
    _remove_file(episode)
    episode.status = "deleted"
    db.add(episode)
    db.commit()


def enforce_retention(db: Session, channel: PodcastChannel) -> int:
    """Trim downloaded episodes to ``PODCAST_KEEP_EPISODES``, oldest first."""
    keep = settings.podcast_keep_episodes
    if keep <= 0:
        return 0

    downloaded = db.scalars(
        select(PodcastEpisode)
        .where(
            PodcastEpisode.channel_id == channel.id,
            PodcastEpisode.status == "completed",
        )
        .order_by(PodcastEpisode.publish_date.desc().nullslast())
    ).all()

    removed = 0
    for episode in downloaded[keep:]:
        _remove_file(episode)
        episode.status = "deleted"
        db.add(episode)
        removed += 1
    if removed:
        db.commit()
        log.info("retention: removed %d episodes from %s", removed, channel.title)
    return removed


# ─── OPML ──────────────────────────────────────────────────────────────────


def import_opml(db: Session, content: str, user: User | None = None) -> list[PodcastChannel]:
    """Bulk-subscribe from an OPML export."""
    from defusedxml.ElementTree import fromstring

    try:
        root = fromstring(content)
    except Exception as exc:
        raise ValueError(f"could not parse OPML: {exc}") from exc

    channels: list[PodcastChannel] = []
    for node in root.iter("outline"):
        url = node.get("xmlUrl") or node.get("xmlurl")
        if not url:
            continue
        try:
            channels.append(add_channel(db, url, user))
        except Exception:
            log.exception("could not subscribe to %s", url)
    return channels


def export_opml(db: Session) -> str:
    """Serialise all subscriptions as OPML."""
    opml = ElementTree.Element("opml", version="2.0")
    head = ElementTree.SubElement(opml, "head")
    ElementTree.SubElement(head, "title").text = "Musicdrome podcast subscriptions"
    body = ElementTree.SubElement(opml, "body")

    for channel in db.scalars(select(PodcastChannel).order_by(PodcastChannel.title)).all():
        ElementTree.SubElement(
            body,
            "outline",
            type="rss",
            text=channel.title or channel.url,
            title=channel.title or channel.url,
            xmlUrl=channel.url,
            htmlUrl=channel.link or "",
        )
    return ElementTree.tostring(opml, encoding="unicode", xml_declaration=True)


# ─── Scheduled refresh ─────────────────────────────────────────────────────


def refresh_all() -> dict[str, int]:
    """Re-read every feed. Called by the scheduler."""
    stats = {"channels": 0, "episodes": 0, "errors": 0}
    if not settings.podcast_enabled:
        return stats

    with session_scope() as db:
        for channel in db.scalars(select(PodcastChannel)).all():
            try:
                stats["episodes"] += refresh_channel(db, channel)
                stats["channels"] += 1
            except Exception as exc:
                log.warning("podcast refresh failed for %s: %s", channel.url, exc)
                stats["errors"] += 1
                db.rollback()
    return stats
