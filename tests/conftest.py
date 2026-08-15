"""Test fixtures.

The environment is set up here, at import time, because ``app.config`` reads
``os.environ`` when it is first imported and pytest imports ``conftest`` before
any test module. Every test therefore runs against a throwaway directory and
never touches a real library or database.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_ROOT = Path(tempfile.mkdtemp(prefix="musicdrome-tests-"))

os.environ.update(
    MUSICDROME_TESTING="1",
    MUSICDROME_DATA_DIR=str(_ROOT / "config"),
    MUSICDROME_MUSIC_DIR=str(_ROOT / "music"),
    EXCLUDE_MUSIC_DIR="",
    LASTFM_API_KEY="",
    LASTFM_USER="",
    LISTENBRAINZ_USER="",
    NAVIDROME_URL="",
    NAVIDROME_USER="",
    NAVIDROME_PASSWORD="",
    AI_PROVIDER="ollama",
    TZ="UTC",
)

import pytest  # noqa: E402

from app import config, db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_database():
    """A brand-new database per test."""
    for suffix in ("", "-wal", "-shm"):
        Path(str(config.DB_PATH) + suffix).unlink(missing_ok=True)
    db.init()
    yield
    for suffix in ("", "-wal", "-shm"):
        Path(str(config.DB_PATH) + suffix).unlink(missing_ok=True)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def play():
    """Insert a play. Usage: ``play("Radiohead", "Karma Police", at=...)``."""
    from app.norm import artist_key, track_key

    def add(artist: str, title: str, at: int | None = None, source: str = "lastfm") -> None:
        with db.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO plays "
                "(artist, title, album, artist_key, track_key, played_at, source) "
                "VALUES (?, ?, '', ?, ?, ?, ?)",
                (artist, title, artist_key(artist), track_key(artist, title),
                 at if at is not None else db.now(), source),
            )

    return add


@pytest.fixture
def navidrome_credentials(monkeypatch):
    """Make ``navidrome.configured()`` true without touching the network."""
    monkeypatch.setattr(config, "NAVIDROME_URL", "http://navidrome.test:4533")
    monkeypatch.setattr(config, "NAVIDROME_USER", "listener")
    monkeypatch.setattr(config, "NAVIDROME_PASSWORD", "hunter2")
    return {"url": "http://navidrome.test:4533", "user": "listener", "password": "hunter2"}


@pytest.fixture
def navidrome_track():
    """Insert a row in navidrome_tracks. Returns the Navidrome song id used."""
    from app.norm import artist_key, track_key

    counter = {"n": 0}

    def add(
        artist: str,
        title: str,
        starred: bool = False,
        play_count: int = 0,
        genre: str = "",
        starred_at: int | None = None,
        rating: int = 0,
    ) -> str:
        counter["n"] += 1
        song_id = f"nd-{counter['n']}"
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO navidrome_tracks (id, artist, title, album, artist_key, "
                "track_key, genre, year, starred, starred_at, rating, play_count, "
                "played_at, synced_at) VALUES (?, ?, ?, '', ?, ?, ?, 0, ?, ?, ?, ?, 0, ?)",
                (
                    song_id, artist, title, artist_key(artist), track_key(artist, title),
                    genre, int(starred),
                    starred_at if starred_at is not None else (db.now() if starred else 0),
                    rating, play_count, db.now(),
                ),
            )
        return song_id

    return add


@pytest.fixture
def suggestion():
    """Insert a suggestion and return its id."""
    from app.norm import track_key

    def add(artist: str, title: str, match: int = 90, status: str = "new", **extra) -> int:
        with db.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO suggestions (track_key, artist, title, album, match, reason, "
                "tags, status, duration, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    track_key(artist, title), artist, title, extra.get("album", ""),
                    match, extra.get("reason", ""), extra.get("tags", ""), status,
                    extra.get("duration", 0), db.now(),
                ),
            )
            return cursor.lastrowid

    return add
