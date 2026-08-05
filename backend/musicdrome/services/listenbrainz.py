"""ListenBrainz client — listen submission and recommendation feeds.

Auth is a per-user token sent as ``Authorization: Token <token>``. Listens are
submitted as ``single`` (a finished play), ``playing_now`` (no timestamp) or
``import`` (a batch of historical plays).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger(__name__)

TIMEOUT = 20.0


class ListenBrainzError(RuntimeError):
    pass


class ListenBrainzClient:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or settings.listenbrainz_api_url).rstrip("/")

    # ─── Transport ────────────────────────────────────────────────────────

    def _post(self, path: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {"Authorization": f"Token {token}", "Content-Type": "application/json"}
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                response = client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ListenBrainzError(f"network error: {exc}") from exc

        if response.status_code >= 400:
            raise ListenBrainzError(
                f"HTTP {response.status_code}: {response.text[:200]}"
            )
        try:
            return response.json()
        except ValueError:
            return {}

    def _get(self, path: str, token: str = "", params: dict | None = None) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {"Authorization": f"Token {token}"} if token else {}
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                response = client.get(url, params=params or {}, headers=headers)
        except httpx.HTTPError as exc:
            raise ListenBrainzError(f"network error: {exc}") from exc
        if response.status_code >= 400:
            raise ListenBrainzError(
                f"HTTP {response.status_code}: {response.text[:200]}"
            )
        try:
            return response.json()
        except ValueError:
            return {}

    # ─── Auth ─────────────────────────────────────────────────────────────

    def validate_token(self, token: str) -> dict[str, Any]:
        data = self._get("1/validate-token", token)
        return {
            "valid": bool(data.get("valid")),
            "username": data.get("user_name", ""),
        }

    # ─── Listen submission ────────────────────────────────────────────────

    @staticmethod
    def _payload(
        artist: str,
        title: str,
        *,
        album: str = "",
        duration: int = 0,
        track_number: int = 0,
        mbid: str = "",
        played_at: datetime | None = None,
    ) -> dict[str, Any]:
        additional: dict[str, Any] = {"media_player": "Musicdrome"}
        if duration:
            additional["duration"] = duration
        if track_number:
            additional["tracknumber"] = track_number
        if mbid:
            additional["recording_mbid"] = mbid

        metadata: dict[str, Any] = {
            "artist_name": artist,
            "track_name": title,
            "additional_info": additional,
        }
        if album:
            metadata["release_name"] = album

        listen: dict[str, Any] = {"track_metadata": metadata}
        if played_at is not None:
            listen["listened_at"] = int(played_at.timestamp())
        return listen

    def submit_listen(
        self,
        token: str,
        artist: str,
        title: str,
        played_at: datetime,
        *,
        album: str = "",
        duration: int = 0,
        track_number: int = 0,
        mbid: str = "",
    ) -> None:
        payload = {
            "listen_type": "single",
            "payload": [
                self._payload(
                    artist, title, album=album, duration=duration,
                    track_number=track_number, mbid=mbid, played_at=played_at,
                )
            ],
        }
        self._post("1/submit-listens", token, payload)

    def submit_playing_now(
        self,
        token: str,
        artist: str,
        title: str,
        *,
        album: str = "",
        duration: int = 0,
        mbid: str = "",
    ) -> None:
        payload = {
            "listen_type": "playing_now",
            "payload": [
                self._payload(
                    artist, title, album=album, duration=duration, mbid=mbid
                )
            ],
        }
        self._post("1/submit-listens", token, payload)

    # ─── Recommendation feeds ─────────────────────────────────────────────

    def user_recommendations(self, username: str, count: int = 50) -> list[dict]:
        """Collaborative-filtering recording recommendations for a user."""
        try:
            data = self._get(
                f"1/cf/recommendation/user/{username}/recording",
                params={"count": count},
            )
        except ListenBrainzError as exc:
            log.debug("listenbrainz recommendations unavailable for %s: %s", username, exc)
            return []

        entries = (data.get("payload", {}) or {}).get("mbids", [])
        return [
            {
                "recording_mbid": entry.get("recording_mbid", ""),
                "score": float(entry.get("score", 0) or 0),
            }
            for entry in entries
            if entry.get("recording_mbid")
        ]

    def similar_artists(self, artist_mbid: str, limit: int = 25) -> list[dict]:
        """Artist neighbours from the ListenBrainz Labs similarity dataset."""
        url = "https://labs.api.listenbrainz.org/similar-artists/json"
        params = {
            "artist_mbids": artist_mbid,
            "algorithm": "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100_filter_True_skip_30",
        }
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                response = client.get(url, params=params)
            if response.status_code != 200:
                return []
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.debug("listenbrainz similar-artists failed: %s", exc)
            return []

        if not isinstance(data, list):
            return []
        results = []
        for entry in data[:limit]:
            name = entry.get("name") or entry.get("artist_name")
            if not name:
                continue
            results.append(
                {
                    "name": name,
                    "mbid": entry.get("artist_mbid", ""),
                    "score": float(entry.get("score", 0) or 0),
                }
            )
        return results

    def fresh_releases(self, username: str, days: int = 30) -> list[dict]:
        try:
            data = self._get(
                f"1/user/{username}/fresh_releases", params={"days": days}
            )
        except ListenBrainzError:
            return []
        releases = (data.get("payload", {}) or {}).get("releases", [])
        return [
            {
                "artist": entry.get("artist_credit_name", ""),
                "album": entry.get("release_name", ""),
                "mbid": entry.get("release_mbid", ""),
                "date": entry.get("release_date", ""),
            }
            for entry in releases
            if entry.get("release_name")
        ]


listenbrainz = ListenBrainzClient()
