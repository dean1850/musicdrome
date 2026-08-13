"""Retrying the network failures that are worth retrying.

Musicdrome is normally run behind a VPN container — the compose file routes it
through gluetun with ``network_mode: service:gluetun`` — and a tunnel that is
still coming up is not a broken configuration, it is a few seconds of weather.
While it establishes, outbound calls fail in two characteristic ways:

    lastfm sync failed: network error: [Errno 99] Cannot assign requested address
    listenbrainz sync failed: network error: [Errno -3] Temporary failure in name resolution

Errno 99 is the source address vanishing with the tunnel; Errno -3 is DNS going
with it. Both clear on their own within seconds. Without a retry they do not
look temporary to anyone: a failed sync writes its error onto the source's
cursor row, so the settings page keeps showing "network error" until the next
successful pass hours later — which is why the fix appears to be "refresh the
page and it sorts itself out".

What is *not* retried matters as much. An HTTP error is a real answer from a
server that heard us, and repeating a rejected API key just rejects it again.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

import httpx

log = logging.getLogger(__name__)

T = TypeVar("T")

# Three attempts over about four seconds. Long enough to cover a tunnel
# handshake, short enough that a genuinely dead network still fails the sync
# rather than hanging the scheduler.
BACKOFF = (1.0, 3.0)


def is_transient(exc: BaseException, *, connect_only: bool = False) -> bool:
    """Whether ``exc`` is the network being briefly unavailable.

    ``httpx.TransportError`` covers the whole family that never reached a
    server: connect errors, DNS failures, timeouts, and the abrupt
    disconnections that surface as "Server disconnected without sending a
    response". Two of its subclasses are excluded because they describe a
    request that is malformed rather than a network that is missing, and no
    amount of waiting improves either.

    With ``connect_only``, a read timeout is *not* transient. That distinction
    exists for the AI backends: a request that timed out waiting for a response
    may well have been received and billed, and quietly sending it again turns
    one slow call into two paid ones.
    """
    if isinstance(exc, (httpx.UnsupportedProtocol, httpx.LocalProtocolError)):
        return False
    if connect_only and isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return False
    return isinstance(exc, httpx.TransportError)


def with_retry(call: Callable[[], T], *, what: str = "", connect_only: bool = False) -> T:
    """Run ``call``, retrying transient network failures with a short backoff.

    The last failure is re-raised unchanged, so callers keep wrapping it in
    their own error type and the message a user eventually sees is the real
    one rather than "retried 3 times".
    """
    for delay in BACKOFF:
        try:
            return call()
        except Exception as exc:
            if not is_transient(exc, connect_only=connect_only):
                raise
            log.debug("%s: %s — retrying in %.0fs", what or "request", exc, delay)
            time.sleep(delay)
    return call()
