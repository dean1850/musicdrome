"""Navidrome roster discovery.

Only the transport is stubbed. What is worth pinning down is the auth scheme
(a salted token, never the password on the wire), the error translation, and
the shape of the response — Subsonic returns a bare object rather than a list
when a server has exactly one user, which is a real one-user household.
"""

import hashlib

import pytest

from app import config, users
from app.sources import navidrome


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setattr(config, "NAVIDROME_URL", "http://navidrome.local:4533")
    monkeypatch.setattr(config, "NAVIDROME_USER", "admin")
    monkeypatch.setattr(config, "NAVIDROME_PASSWORD", "hunter2")


def respond(monkeypatch, payload, status_code=200):
    """Stand in for the whole httpx client, capturing the request params."""
    captured = {}

    class FakeResponse:
        def __init__(self):
            self.status_code = status_code

        def json(self):
            return payload

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            captured["url"] = url
            captured["params"] = params or {}
            return FakeResponse()

    monkeypatch.setattr(navidrome.httpx, "Client", FakeClient)
    return captured


def ok(users_payload):
    return {"subsonic-response": {"status": "ok", "version": "1.16.1", **users_payload}}


# ─── Auth ──────────────────────────────────────────────────────────────────


def test_the_password_is_never_sent(monkeypatch):
    captured = respond(monkeypatch, ok({"users": {"user": []}}))
    navidrome.users()

    params = captured["params"]
    assert "hunter2" not in str(params), "the password must never go on the wire"
    assert params["t"] == hashlib.md5(f"hunter2{params['s']}".encode()).hexdigest()
    assert params["u"] == "admin"


def test_the_salt_changes_between_calls(monkeypatch):
    captured = respond(monkeypatch, ok({"users": {"user": []}}))
    navidrome.users()
    first = captured["params"]["s"]
    navidrome.users()
    assert captured["params"]["s"] != first


def test_not_configured_without_credentials(monkeypatch):
    monkeypatch.setattr(config, "NAVIDROME_URL", "")
    assert navidrome.configured() is False
    with pytest.raises(navidrome.NavidromeError, match="must all be set"):
        navidrome.users()


# ─── Responses ─────────────────────────────────────────────────────────────


def test_users_are_returned_with_names_and_mail(monkeypatch):
    respond(monkeypatch, ok({"users": {"user": [
        {"username": "alex", "email": "alex@home", "adminRole": True},
        {"username": "sam", "email": ""},
    ]}}))

    assert navidrome.users() == [
        {"name": "alex", "email": "alex@home"},
        {"name": "sam", "email": ""},
    ]


def test_a_single_user_server_returns_an_object_not_a_list(monkeypatch):
    """Subsonic collapses a one-element list, which would otherwise iterate
    over the dict's keys and produce nonsense."""
    respond(monkeypatch, ok({"users": {"user": {"username": "alex", "email": "a@h"}}}))
    assert navidrome.users() == [{"name": "alex", "email": "a@h"}]


def test_an_empty_server_is_not_an_error(monkeypatch):
    respond(monkeypatch, ok({"users": {}}))
    assert navidrome.users() == []


# ─── Errors ────────────────────────────────────────────────────────────────


def test_bad_credentials_say_so(monkeypatch):
    respond(monkeypatch, {"subsonic-response": {
        "status": "failed", "error": {"code": 40, "message": "Wrong username or password"}}})

    with pytest.raises(navidrome.NavidromeError, match="rejected the username or password"):
        navidrome.users()


def test_a_non_admin_account_is_explained(monkeypatch):
    """The most likely misconfiguration, and Subsonic's own message for it is
    not obviously about admin rights."""
    respond(monkeypatch, {"subsonic-response": {
        "status": "failed", "error": {"code": 50, "message": "User is not authorized"}}})

    with pytest.raises(navidrome.NavidromeError, match="not an admin"):
        navidrome.users()


def test_a_non_json_response_suggests_the_url_is_wrong(monkeypatch):
    class NotJson:
        status_code = 200

        def json(self):
            raise ValueError("nope")

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            return NotJson()

    monkeypatch.setattr(navidrome.httpx, "Client", FakeClient)
    with pytest.raises(navidrome.NavidromeError, match="is the URL right"):
        navidrome.users()


# ─── Import ────────────────────────────────────────────────────────────────


def test_discovering_users_adds_them(client, monkeypatch):
    monkeypatch.setattr(navidrome, "users", lambda: [{"name": "alex", "email": "alex@home"}])

    body = client.post("/api/users/discover").json()
    assert body["added"] == ["alex"]
    assert "alex" in [user["name"] for user in users.all_users()]


def test_a_discovery_failure_becomes_a_readable_error(client, monkeypatch):
    def boom():
        raise navidrome.NavidromeError("Navidrome rejected the username or password")

    monkeypatch.setattr(navidrome, "users", boom)
    response = client.post("/api/users/discover")

    assert response.status_code == 400
    assert "rejected" in response.json()["detail"]
