"""MusicBrainz client.

The public MusicBrainz server allows one request per second per client and
requires a descriptive User-Agent. Both are enforced here rather than left to
callers — a process-wide lock serialises requests and sleeps out the remainder
of the window.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger(__name__)

TIMEOUT = 20.0


class _RateLimiter:
    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._last_call = 0.0

    def wait(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last_call
            remaining = self._min_interval - elapsed
            if remaining > 0:
                time.sleep(remaining)
            self._last_call = time.monotonic()


class MusicBrainzClient:
    def __init__(self) -> None:
        self._limiter = _RateLimiter(settings.musicbrainz_rate_limit)

    @property
    def enabled(self) -> bool:
        return settings.musicbrainz_enabled

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if not self.enabled:
            return None

        self._limiter.wait()
        query = {"fmt": "json", **{k: v for k, v in params.items() if v not in (None, "")}}
        url = f"{settings.musicbrainz_api_url.rstrip('/')}/{path.lstrip('/')}"
        headers = {"User-Agent": settings.musicbrainz_user_agent}

        try:
            with httpx.Client(timeout=TIMEOUT, headers=headers) as client:
                response = client.get(url, params=query)
            if response.status_code == 503:
                # Rate limited despite our pacing — back off once and retry.
                time.sleep(2.0)
                self._limiter.wait()
                with httpx.Client(timeout=TIMEOUT, headers=headers) as client:
                    response = client.get(url, params=query)
            if response.status_code != 200:
                log.debug("musicbrainz %s -> %s", path, response.status_code)
                return None
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.debug("musicbrainz request failed (%s): %s", path, exc)
            return None

    # ─── Lookups ──────────────────────────────────────────────────────────

    def artist(self, mbid: str) -> dict[str, Any] | None:
        return self._get(
            f"artist/{mbid}", {"inc": "url-rels+tags+genres+aliases"}
        )

    def release(self, mbid: str) -> dict[str, Any] | None:
        return self._get(
            f"release/{mbid}", {"inc": "artists+recordings+release-groups+genres"}
        )

    def recording(self, mbid: str) -> dict[str, Any] | None:
        return self._get(f"recording/{mbid}", {"inc": "artists+releases+genres"})

    # ─── Searches ─────────────────────────────────────────────────────────

    def search_artist(self, name: str, limit: int = 5) -> list[dict[str, Any]]:
        data = self._get("artist", {"query": f'artist:"{name}"', "limit": limit})
        return (data or {}).get("artists", [])

    def search_release(
        self, artist: str, album: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        query = f'release:"{album}" AND artist:"{artist}"'
        data = self._get("release", {"query": query, "limit": limit})
        return (data or {}).get("releases", [])

    def search_recording(
        self, artist: str, title: str, album: str = "", limit: int = 5
    ) -> list[dict[str, Any]]:
        query = f'recording:"{title}" AND artist:"{artist}"'
        if album:
            query += f' AND release:"{album}"'
        data = self._get("recording", {"query": query, "limit": limit})
        return (data or {}).get("recordings", [])

    # ─── Convenience resolvers ────────────────────────────────────────────

    def resolve_artist_mbid(self, name: str) -> str:
        results = self.search_artist(name, limit=1)
        if not results:
            return ""
        best = results[0]
        # MusicBrainz scores 0-100; anything below 80 is usually a wrong match.
        if int(best.get("score", 0)) < 80:
            return ""
        return best.get("id", "")

    def resolve_track(self, artist: str, title: str, album: str = "") -> dict[str, str]:
        """Best-effort identification of a track, for tagging downloads."""
        results = self.search_recording(artist, title, album, limit=5)
        if not results:
            return {}

        best = results[0]
        if int(best.get("score", 0)) < 75:
            return {}

        artist_credit = best.get("artist-credit", []) or []
        artist_name = artist_credit[0]["name"] if artist_credit else artist
        artist_mbid = (
            (artist_credit[0].get("artist", {}) or {}).get("id", "")
            if artist_credit
            else ""
        )

        releases = best.get("releases", []) or []
        release = releases[0] if releases else {}

        return {
            "recording_mbid": best.get("id", ""),
            "title": best.get("title", title),
            "artist": artist_name,
            "artist_mbid": artist_mbid,
            "album": release.get("title", album),
            "release_mbid": release.get("id", ""),
            "date": release.get("date", ""),
            "score": str(best.get("score", 0)),
        }

    def artist_genres(self, mbid: str) -> list[str]:
        data = self.artist(mbid)
        if not data:
            return []
        genres = [g.get("name", "") for g in data.get("genres", []) or []]
        if not genres:
            genres = [t.get("name", "") for t in data.get("tags", []) or []]
        return [g for g in genres if g]


musicbrainz = MusicBrainzClient()
