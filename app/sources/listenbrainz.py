"""ListenBrainz — listening history.

Reads only. A token is optional and needed just for a profile that is not
public; the listens endpoint is open otherwise.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

import httpx

from .. import config, net

log = logging.getLogger(__name__)

TIMEOUT = 20.0
PAGE_SIZE = 100  # the endpoint's documented maximum


class ListenBrainzError(RuntimeError):
    pass


def configured(user: str = "") -> bool:
    return bool(user or config.LISTENBRAINZ_USER)


def _get(path: str, params: dict[str, Any], token: str = "") -> dict[str, Any]:
    url = f"{config.LISTENBRAINZ_API_URL.rstrip('/')}/{path.lstrip('/')}"
    token = token or config.LISTENBRAINZ_TOKEN
    headers = {"Authorization": f"Token {token}"} if token else {}
    def fetch() -> httpx.Response:
        with httpx.Client(timeout=TIMEOUT) as client:
            return client.get(url, params=params, headers=headers)

    try:
        response = net.with_retry(fetch, what=f"listenbrainz {path}")
    except httpx.HTTPError as exc:
        raise ListenBrainzError(f"network error: {exc}") from exc

    if response.status_code >= 400:
        raise ListenBrainzError(f"HTTP {response.status_code}: {response.text[:200]}")
    try:
        return response.json()
    except ValueError as exc:
        raise ListenBrainzError("invalid response") from exc


def recent_tracks(
    since: int = 0, max_pages: int = 25, user: str = "", token: str = ""
) -> Iterator[dict[str, Any]]:
    """Yield plays newer than ``since`` (unix seconds).

    ``user`` and ``token`` name whose listens to read, falling back to the
    environment's so a single-user install works with no user rows at all. The
    token is only needed for a profile that is not public.

    Listens come back newest-first, and the endpoint accepts ``max_ts`` *or*
    ``min_ts`` but **never both in one call**. So rather than bounding the
    window from both ends, this walks backwards from the newest listen using
    ``max_ts`` alone and stops as soon as it reaches ``since`` — which cannot
    trip the constraint, and needs no cursor arithmetic.
    """
    max_ts = 0
    for _ in range(max_pages):
        params: dict[str, Any] = {"count": PAGE_SIZE}
        if max_ts:
            params["max_ts"] = max_ts

        data = _get(f"1/user/{user or config.LISTENBRAINZ_USER}/listens", params, token=token)
        listens = (data.get("payload") or {}).get("listens") or []
        if not listens:
            return

        oldest = None
        for listen in listens:
            played_at = int(listen.get("listened_at") or 0)
            if not played_at:
                continue
            if played_at <= since:
                return  # caught up: everything from here down is already stored

            oldest = played_at if oldest is None else min(oldest, played_at)
            metadata = listen.get("track_metadata") or {}
            artist = (metadata.get("artist_name") or "").strip()
            title = (metadata.get("track_name") or "").strip()
            if not artist or not title:
                continue
            yield {
                "artist": artist,
                "title": title,
                "album": (metadata.get("release_name") or "").strip(),
                "played_at": played_at,
                "source": "listenbrainz",
            }

        if oldest is None or len(listens) < PAGE_SIZE:
            return
        max_ts = oldest  # exclusive upper bound for the next page
