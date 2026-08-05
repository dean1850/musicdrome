"""Last.fm client — metadata lookups and scrobbling.

Write methods (auth, now-playing, scrobble) must be POSTed with an ``api_sig``
built from every parameter sorted by name, concatenated as ``key + value`` and
md5'd together with the shared secret. Read methods only need the API key.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger(__name__)

API_ROOT = "https://ws.audioscrobbler.com/2.0/"
TIMEOUT = 15.0


class LastFmError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"Last.fm error {code}: {message}")
        self.code = code
        self.message = message


class LastFmClient:
    def __init__(
        self, api_key: str | None = None, api_secret: str | None = None
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.lastfm_api_key
        self.api_secret = (
            api_secret if api_secret is not None else settings.lastfm_api_secret
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def can_write(self) -> bool:
        return bool(self.api_key and self.api_secret)

    # ─── Signing ──────────────────────────────────────────────────────────

    def _sign(self, params: dict[str, Any]) -> str:
        parts = "".join(
            f"{key}{params[key]}"
            for key in sorted(params)
            if key not in {"format", "callback"}
        )
        return hashlib.md5((parts + self.api_secret).encode("utf-8")).hexdigest()

    # ─── Transport ────────────────────────────────────────────────────────

    def _request(
        self, method: str, params: dict[str, Any], *, write: bool = False
    ) -> dict[str, Any]:
        if not self.configured:
            raise LastFmError(6, "Last.fm API key is not configured")

        payload = {k: v for k, v in params.items() if v not in (None, "")}
        payload["method"] = method
        payload["api_key"] = self.api_key

        if write:
            if not self.api_secret:
                raise LastFmError(6, "Last.fm API secret is not configured")
            payload["api_sig"] = self._sign(payload)
        payload["format"] = "json"

        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                if write:
                    response = client.post(API_ROOT, data=payload)
                else:
                    response = client.get(API_ROOT, params=payload)
        except httpx.HTTPError as exc:
            raise LastFmError(-1, f"network error: {exc}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise LastFmError(-1, f"invalid response: {response.text[:200]}") from exc

        if isinstance(data, dict) and "error" in data:
            raise LastFmError(int(data.get("error", 0)), str(data.get("message", "")))
        return data

    # ─── Auth ─────────────────────────────────────────────────────────────

    def get_mobile_session(self, username: str, password: str) -> dict[str, str]:
        """Exchange a username/password for a durable session key."""
        data = self._request(
            "auth.getMobileSession",
            {"username": username, "password": password},
            write=True,
        )
        session = data.get("session", {})
        return {
            "key": session.get("key", ""),
            "name": session.get("name", username),
        }

    # ─── Scrobbling ───────────────────────────────────────────────────────

    def update_now_playing(
        self,
        session_key: str,
        artist: str,
        title: str,
        *,
        album: str = "",
        album_artist: str = "",
        duration: int = 0,
        track_number: int = 0,
        mbid: str = "",
    ) -> None:
        self._request(
            "track.updateNowPlaying",
            {
                "artist": artist,
                "track": title,
                "album": album or None,
                "albumArtist": album_artist or None,
                "duration": duration or None,
                "trackNumber": track_number or None,
                "mbid": mbid or None,
                "sk": session_key,
            },
            write=True,
        )

    def scrobble(
        self,
        session_key: str,
        artist: str,
        title: str,
        played_at: datetime,
        *,
        album: str = "",
        album_artist: str = "",
        duration: int = 0,
        track_number: int = 0,
        mbid: str = "",
    ) -> None:
        self._request(
            "track.scrobble",
            {
                "artist[0]": artist,
                "track[0]": title,
                "timestamp[0]": int(played_at.timestamp()),
                "album[0]": album or None,
                "albumArtist[0]": album_artist or None,
                "duration[0]": duration or None,
                "trackNumber[0]": track_number or None,
                "mbid[0]": mbid or None,
                "sk": session_key,
            },
            write=True,
        )

    def love(self, session_key: str, artist: str, title: str, loved: bool = True) -> None:
        self._request(
            "track.love" if loved else "track.unlove",
            {"artist": artist, "track": title, "sk": session_key},
            write=True,
        )

    # ─── Metadata ─────────────────────────────────────────────────────────

    def artist_info(self, artist: str, mbid: str = "") -> dict[str, Any] | None:
        try:
            data = self._request(
                "artist.getInfo",
                {
                    "artist": artist if not mbid else None,
                    "mbid": mbid or None,
                    "lang": settings.lastfm_language,
                    "autocorrect": 1,
                },
            )
        except LastFmError as exc:
            log.debug("artist.getInfo failed for %s: %s", artist, exc)
            return None

        info = data.get("artist")
        if not info:
            return None

        images = info.get("image", []) or []
        image_url = ""
        for image in reversed(images):
            if image.get("#text"):
                image_url = image["#text"]
                break

        stats = info.get("stats", {}) or {}
        bio = (info.get("bio", {}) or {}).get("content", "")
        return {
            "name": info.get("name", artist),
            "mbid": info.get("mbid", ""),
            "url": info.get("url", ""),
            "image_url": image_url,
            "biography": bio,
            "listeners": int(stats.get("listeners", 0) or 0),
            "playcount": int(stats.get("playcount", 0) or 0),
            "tags": [t.get("name", "") for t in (info.get("tags", {}) or {}).get("tag", [])],
            "similar": [
                s.get("name", "")
                for s in (info.get("similar", {}) or {}).get("artist", [])
            ],
        }

    def similar_artists(self, artist: str, mbid: str = "", limit: int = 30) -> list[dict]:
        try:
            data = self._request(
                "artist.getSimilar",
                {
                    "artist": artist if not mbid else None,
                    "mbid": mbid or None,
                    "limit": limit,
                    "autocorrect": 1,
                },
            )
        except LastFmError as exc:
            log.debug("artist.getSimilar failed for %s: %s", artist, exc)
            return []

        entries = (data.get("similarartists", {}) or {}).get("artist", [])
        if isinstance(entries, dict):
            entries = [entries]
        return [
            {
                "name": entry.get("name", ""),
                "mbid": entry.get("mbid", ""),
                "score": float(entry.get("match", 0) or 0),
                "url": entry.get("url", ""),
            }
            for entry in entries
            if entry.get("name")
        ]

    def similar_tracks(
        self, artist: str, title: str, mbid: str = "", limit: int = 30
    ) -> list[dict]:
        try:
            data = self._request(
                "track.getSimilar",
                {
                    "artist": artist if not mbid else None,
                    "track": title if not mbid else None,
                    "mbid": mbid or None,
                    "limit": limit,
                    "autocorrect": 1,
                },
            )
        except LastFmError as exc:
            log.debug("track.getSimilar failed for %s - %s: %s", artist, title, exc)
            return []

        entries = (data.get("similartracks", {}) or {}).get("track", [])
        if isinstance(entries, dict):
            entries = [entries]
        return [
            {
                "title": entry.get("name", ""),
                "artist": (entry.get("artist", {}) or {}).get("name", ""),
                "mbid": entry.get("mbid", ""),
                "score": float(entry.get("match", 0) or 0),
            }
            for entry in entries
            if entry.get("name")
        ]

    def top_tracks(self, artist: str, limit: int = 20) -> list[dict]:
        try:
            data = self._request(
                "artist.getTopTracks", {"artist": artist, "limit": limit, "autocorrect": 1}
            )
        except LastFmError:
            return []
        entries = (data.get("toptracks", {}) or {}).get("track", [])
        if isinstance(entries, dict):
            entries = [entries]
        return [
            {
                "title": entry.get("name", ""),
                "artist": (entry.get("artist", {}) or {}).get("name", artist),
                "mbid": entry.get("mbid", ""),
                "playcount": int(entry.get("playcount", 0) or 0),
            }
            for entry in entries
            if entry.get("name")
        ]

    def album_info(self, artist: str, album: str) -> dict[str, Any] | None:
        try:
            data = self._request(
                "album.getInfo", {"artist": artist, "album": album, "autocorrect": 1}
            )
        except LastFmError:
            return None
        info = data.get("album")
        if not info:
            return None
        images = info.get("image", []) or []
        image_url = next(
            (i["#text"] for i in reversed(images) if i.get("#text")), ""
        )
        return {
            "name": info.get("name", album),
            "artist": info.get("artist", artist),
            "mbid": info.get("mbid", ""),
            "url": info.get("url", ""),
            "image_url": image_url,
            "listeners": int(info.get("listeners", 0) or 0),
            "playcount": int(info.get("playcount", 0) or 0),
            "description": ((info.get("wiki", {}) or {}).get("content", "")),
            "tags": [
                t.get("name", "") for t in (info.get("tags", {}) or {}).get("tag", [])
            ],
        }

    def user_top_artists(self, username: str, period: str = "overall", limit: int = 50) -> list[dict]:
        try:
            data = self._request(
                "user.getTopArtists",
                {"user": username, "period": period, "limit": limit},
            )
        except LastFmError:
            return []
        entries = (data.get("topartists", {}) or {}).get("artist", [])
        if isinstance(entries, dict):
            entries = [entries]
        return [
            {
                "name": entry.get("name", ""),
                "mbid": entry.get("mbid", ""),
                "playcount": int(entry.get("playcount", 0) or 0),
            }
            for entry in entries
            if entry.get("name")
        ]

    def user_recommended(self, username: str, limit: int = 50) -> list[dict]:
        """Recommendations derived from the user's own top artists' neighbours.

        Last.fm retired the public ``user.getRecommendedArtists`` endpoint, so
        this walks the similarity graph out one hop instead.
        """
        seeds = self.user_top_artists(username, period="3month", limit=15)
        seen: dict[str, dict] = {}
        for seed in seeds:
            for similar in self.similar_artists(seed["name"], seed.get("mbid", ""), limit=10):
                name = similar["name"]
                if name in seen:
                    seen[name]["score"] = max(seen[name]["score"], similar["score"])
                    continue
                seen[name] = {
                    "name": name,
                    "mbid": similar.get("mbid", ""),
                    "score": similar["score"],
                    "seed": seed["name"],
                }
        ranked = sorted(seen.values(), key=lambda item: item["score"], reverse=True)
        return ranked[:limit]


lastfm = LastFmClient()
