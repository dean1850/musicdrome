"""Lidarr integration — two-way sync.

Division of labour: Lidarr owns indexers and the download client (torrent or
usenet); Musicdrome owns the library and the recommendations. So the sync runs
in two directions:

* **push** — approved wanted items whose provider is ``lidarr`` are resolved to
  a MusicBrainz artist and added to Lidarr as monitored, optionally kicking off
  an immediate indexer search.
* **pull** — Lidarr's import history is polled for newly imported tracks, and
  the paths it reports are handed to the scanner for a targeted rescan rather
  than waiting for the next full scan.

Both directions are individually switchable (``LIDARR_PUSH_WANTED`` /
``LIDARR_PULL_IMPORTED``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from ..config import settings
from ..db import session_scope, utcnow
from ..models import WantedItem, WantedStatus
from . import scanner
from .musicbrainz import musicbrainz

log = logging.getLogger(__name__)

TIMEOUT = 30.0


class LidarrError(RuntimeError):
    pass


class LidarrClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
        self.base_url = (base_url or settings.lidarr_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.lidarr_api_key

    @property
    def configured(self) -> bool:
        return bool(settings.lidarr_enabled and self.base_url and self.api_key)

    # ─── Transport ────────────────────────────────────────────────────────

    def _request(
        self, method: str, path: str, *, params: dict | None = None, json: Any = None
    ) -> Any:
        if not self.api_key:
            raise LidarrError("LIDARR_API_KEY is not set")

        url = f"{self.base_url}/api/v1/{path.lstrip('/')}"
        headers = {"X-Api-Key": self.api_key, "Accept": "application/json"}
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                response = client.request(
                    method, url, params=params, json=json, headers=headers
                )
        except httpx.HTTPError as exc:
            raise LidarrError(f"cannot reach Lidarr at {self.base_url}: {exc}") from exc

        if response.status_code >= 400:
            raise LidarrError(
                f"Lidarr {method} {path} returned {response.status_code}: "
                f"{response.text[:300]}"
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    # ─── Reads ────────────────────────────────────────────────────────────

    def system_status(self) -> dict:
        return self._request("GET", "system/status") or {}

    def test_connection(self) -> dict:
        status = self.system_status()
        return {
            "connected": bool(status),
            "version": status.get("version", ""),
            "app_name": status.get("appName", "Lidarr"),
        }

    def root_folders(self) -> list[dict]:
        return self._request("GET", "rootfolder") or []

    def quality_profiles(self) -> list[dict]:
        return self._request("GET", "qualityprofile") or []

    def metadata_profiles(self) -> list[dict]:
        return self._request("GET", "metadataprofile") or []

    def artists(self) -> list[dict]:
        return self._request("GET", "artist") or []

    def lookup_artist(self, term: str) -> list[dict]:
        return self._request("GET", "artist/lookup", params={"term": term}) or []

    def albums(self, artist_id: int) -> list[dict]:
        return self._request("GET", "album", params={"artistId": artist_id}) or []

    def lookup_album(self, term: str) -> list[dict]:
        return self._request("GET", "album/lookup", params={"term": term}) or []

    def history(self, page_size: int = 100) -> list[dict]:
        data = self._request(
            "GET",
            "history",
            params={
                "page": 1,
                "pageSize": page_size,
                "sortKey": "date",
                "sortDirection": "descending",
            },
        )
        if isinstance(data, dict):
            return data.get("records", []) or []
        return data or []

    def queue(self) -> list[dict]:
        data = self._request("GET", "queue", params={"pageSize": 100})
        if isinstance(data, dict):
            return data.get("records", []) or []
        return data or []

    # ─── Writes ───────────────────────────────────────────────────────────

    def add_artist(
        self,
        *,
        artist_name: str,
        foreign_artist_id: str,
        root_folder: str | None = None,
        quality_profile_id: int | None = None,
        metadata_profile_id: int | None = None,
        monitor: str | None = None,
        search: bool | None = None,
        extra: dict | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "artistName": artist_name,
            "foreignArtistId": foreign_artist_id,
            "rootFolderPath": root_folder or settings.lidarr_root_folder,
            "qualityProfileId": quality_profile_id or settings.lidarr_quality_profile_id,
            "metadataProfileId": metadata_profile_id or settings.lidarr_metadata_profile_id,
            "monitored": True,
            "addOptions": {
                "monitor": monitor or settings.lidarr_monitor_mode,
                "searchForMissingAlbums": (
                    settings.lidarr_search_on_add if search is None else search
                ),
            },
        }
        # Lidarr's lookup payload carries fields it wants echoed back verbatim
        if extra:
            for key in ("artistType", "disambiguation", "overview", "images", "links"):
                if key in extra:
                    payload[key] = extra[key]
        return self._request("POST", "artist", json=payload) or {}

    def command(self, name: str, **kwargs) -> dict:
        return self._request("POST", "command", json={"name": name, **kwargs}) or {}

    def search_album(self, album_ids: list[int]) -> dict:
        return self.command("AlbumSearch", albumIds=album_ids)

    def search_artist(self, artist_id: int) -> dict:
        return self.command("ArtistSearch", artistId=artist_id)

    def refresh_artist(self, artist_id: int | None = None) -> dict:
        if artist_id is None:
            return self.command("RefreshArtist")
        return self.command("RefreshArtist", artistId=artist_id)


lidarr = LidarrClient()


# ─── Push: wanted → Lidarr ─────────────────────────────────────────────────


def _resolve_foreign_id(client: LidarrClient, item: WantedItem) -> tuple[str, str, dict]:
    """Find the MusicBrainz artist ID Lidarr needs, plus its lookup payload."""
    if item.mbid and item.item_type == "artist":
        results = client.lookup_artist(f"lidarr:{item.mbid}")
        if results:
            best = results[0]
            return best.get("foreignArtistId", ""), best.get("artistName", ""), best

    term = item.artist_name.strip()
    if not term:
        return "", "", {}

    results = client.lookup_artist(term)
    for candidate in results:
        if candidate.get("artistName", "").lower() == term.lower():
            return candidate.get("foreignArtistId", ""), candidate.get("artistName", ""), candidate
    if results:
        best = results[0]
        return best.get("foreignArtistId", ""), best.get("artistName", ""), best

    # Fall back to MusicBrainz directly when Lidarr's lookup comes back empty
    mbid = musicbrainz.resolve_artist_mbid(term)
    return (mbid, term, {}) if mbid else ("", "", {})


def push_wanted(limit: int = 20) -> dict[str, int]:
    """Send approved Lidarr-provider wanted items to Lidarr."""
    stats = {"pushed": 0, "skipped": 0, "failed": 0}
    client = lidarr
    if not client.configured or not settings.lidarr_push_wanted:
        return stats

    try:
        existing_ids = {
            artist.get("foreignArtistId")
            for artist in client.artists()
            if artist.get("foreignArtistId")
        }
    except LidarrError as exc:
        log.warning("cannot list Lidarr artists: %s", exc)
        return stats

    with session_scope() as db:
        items = db.scalars(
            select(WantedItem)
            .where(
                WantedItem.provider == "lidarr",
                WantedItem.status == WantedStatus.APPROVED.value,
            )
            .order_by(WantedItem.created_at.asc())
            .limit(limit)
        ).all()

        for item in items:
            try:
                foreign_id, name, payload = _resolve_foreign_id(client, item)
                if not foreign_id:
                    item.status = WantedStatus.FAILED.value
                    item.error_message = f"could not resolve '{item.artist_name}' to a MusicBrainz artist"
                    stats["failed"] += 1
                    db.add(item)
                    continue

                if foreign_id in existing_ids:
                    # Already monitored — treat the push as done and let the
                    # pull side pick the release up when it imports.
                    item.status = WantedStatus.DOWNLOADING.value
                    item.external_id = foreign_id
                    item.decided_at = item.decided_at or utcnow()
                    stats["skipped"] += 1
                    db.add(item)
                    continue

                result = client.add_artist(
                    artist_name=name or item.artist_name,
                    foreign_artist_id=foreign_id,
                    extra=payload,
                )
                item.status = WantedStatus.DOWNLOADING.value
                item.external_id = str(result.get("id") or foreign_id)
                item.error_message = None
                item.decided_at = item.decided_at or utcnow()
                existing_ids.add(foreign_id)
                stats["pushed"] += 1
                db.add(item)
                log.info("pushed %s to Lidarr", name or item.artist_name)
            except LidarrError as exc:
                item.status = WantedStatus.FAILED.value
                item.error_message = str(exc)[:500]
                stats["failed"] += 1
                db.add(item)
                log.warning("Lidarr push failed for %s: %s", item.artist_name, exc)
        db.commit()

    return stats


# ─── Pull: Lidarr → library ────────────────────────────────────────────────

_IMPORT_EVENTS = {"trackfileimported", "downloadfolderimported", "artistfolderimported"}


def _import_paths(records: list[dict]) -> set[Path]:
    """Extract on-disk paths from Lidarr history records."""
    paths: set[Path] = set()
    for record in records:
        event = str(record.get("eventType", "")).lower()
        if event not in _IMPORT_EVENTS:
            continue
        data = record.get("data") or {}
        for key in ("importedPath", "path", "droppedPath"):
            value = data.get(key)
            if value:
                paths.add(Path(value).parent)
                break
    return paths


def pull_imported(page_size: int = 100) -> dict[str, int]:
    """Poll Lidarr for imports and rescan just those folders."""
    stats = {"paths": 0, "added": 0, "updated": 0}
    client = lidarr
    if not client.configured or not settings.lidarr_pull_imported:
        return stats

    try:
        records = client.history(page_size=page_size)
    except LidarrError as exc:
        log.warning("cannot read Lidarr history: %s", exc)
        return stats

    paths = _import_paths(records)
    # Lidarr sees the library at LIDARR_ROOT_FOLDER; only rescan paths that
    # actually exist from Musicdrome's side of the mount.
    existing = [path for path in paths if path.exists()]
    stats["paths"] = len(existing)

    if not existing:
        return stats

    result = scanner.scan_paths(existing)
    stats["added"] = result.added
    stats["updated"] = result.updated

    if result.added or result.updated:
        log.info(
            "Lidarr import: %d new / %d updated tracks from %d folders",
            result.added, result.updated, len(existing),
        )
        _mark_imported(existing)
    return stats


def _mark_imported(paths: list[Path]) -> None:
    """Close out wanted items whose artist now has files on disk."""
    with session_scope() as db:
        pending = db.scalars(
            select(WantedItem).where(
                WantedItem.provider == "lidarr",
                WantedItem.status == WantedStatus.DOWNLOADING.value,
            )
        ).all()
        for item in pending:
            needle = item.artist_name.lower()
            if not needle:
                continue
            if any(needle in str(path).lower() for path in paths):
                item.status = WantedStatus.IMPORTED.value
                item.completed_at = utcnow()
                item.result_path = str(paths[0])
                db.add(item)
        db.commit()


def sync() -> dict[str, dict]:
    """Full two-way pass. Called by the scheduler."""
    if not lidarr.configured:
        return {"push": {}, "pull": {}}
    return {"push": push_wanted(), "pull": pull_imported()}
