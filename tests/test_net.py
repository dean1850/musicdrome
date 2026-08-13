"""Retrying the network, and knowing when not to."""

from __future__ import annotations

import httpx
import pytest

from app import net


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """The backoff is real time; the tests only care that it was attempted."""
    slept = []
    monkeypatch.setattr(net.time, "sleep", slept.append)
    return slept


def _raises(*errors):
    """A call that raises each error in turn, then returns "ok"."""
    queue = list(errors)

    def call():
        if queue:
            raise queue.pop(0)
        return "ok"

    return call


# The two failures from the Musicdrome logs, verbatim: a tunnel whose source
# address has gone, and a tunnel whose DNS has gone with it.
GONE_ADDRESS = httpx.ConnectError("[Errno 99] Cannot assign requested address")
GONE_DNS = httpx.ConnectError("[Errno -3] Temporary failure in name resolution")


@pytest.mark.parametrize("failure", [
    GONE_ADDRESS,
    GONE_DNS,
    httpx.ConnectTimeout("timed out"),
    httpx.RemoteProtocolError("Server disconnected without sending a response."),
    httpx.ReadTimeout("timed out"),
])
def test_transient_failures_are_retried(failure, no_waiting):
    assert net.with_retry(_raises(failure)) == "ok"
    assert len(no_waiting) == 1


def test_it_gives_up_and_re_raises_the_real_error(no_waiting):
    """The message a user reads should be the network's, not the retry loop's."""
    call = _raises(GONE_ADDRESS, GONE_ADDRESS, GONE_ADDRESS)
    with pytest.raises(httpx.ConnectError, match="Errno 99"):
        net.with_retry(call)
    assert len(no_waiting) == len(net.BACKOFF)


@pytest.mark.parametrize("failure", [
    # A server that answered is not a network that is missing.
    httpx.HTTPStatusError("403", request=None, response=None),
    httpx.UnsupportedProtocol("unknown scheme"),
    httpx.LocalProtocolError("malformed request"),
    ValueError("not a network error at all"),
])
def test_real_answers_and_bad_requests_are_not_retried(failure, no_waiting):
    with pytest.raises(type(failure)):
        net.with_retry(_raises(failure))
    assert no_waiting == []


def test_connect_only_does_not_repeat_a_request_that_may_have_been_billed(no_waiting):
    """A read timeout means the provider heard us and may already be charging."""
    with pytest.raises(httpx.ReadTimeout):
        net.with_retry(_raises(httpx.ReadTimeout("timed out")), connect_only=True)
    assert no_waiting == []


def test_connect_only_still_retries_what_never_arrived(no_waiting):
    assert net.with_retry(_raises(GONE_ADDRESS), connect_only=True) == "ok"
    assert len(no_waiting) == 1


def test_a_call_that_works_is_not_retried(no_waiting):
    assert net.with_retry(lambda: "ok") == "ok"
    assert no_waiting == []
