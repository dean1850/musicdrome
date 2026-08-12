"""Upgrading a single-user database in place.

This is the one change in the multi-user work that can destroy data rather than
merely misbehave: two tables have their UNIQUE constraint altered, which SQLite
can only do by rebuilding them. So the test builds a database in the *old*
shape, with rows in it, and checks that everything survives and lands on the
first user.

The old schema is written out here in full rather than imported, because the
point is to test against what is actually on disk in an existing install — a
definition that has by now been edited out of the source.
"""

import sqlite3

import pytest

from app import config, db, users

OLD_SCHEMA = """
CREATE TABLE plays (
    id          INTEGER PRIMARY KEY,
    artist      TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    album       TEXT    NOT NULL DEFAULT '',
    artist_key  TEXT    NOT NULL,
    track_key   TEXT    NOT NULL,
    played_at   INTEGER NOT NULL,
    source      TEXT    NOT NULL,
    UNIQUE (track_key, played_at, source)
);
CREATE TABLE scans (
    id            INTEGER PRIMARY KEY,
    started_at    INTEGER NOT NULL,
    finished_at   INTEGER,
    status        TEXT    NOT NULL DEFAULT 'running',
    trigger       TEXT    NOT NULL DEFAULT 'manual',
    provider      TEXT    NOT NULL DEFAULT '',
    model         TEXT    NOT NULL DEFAULT '',
    requested     INTEGER NOT NULL DEFAULT 0,
    returned      INTEGER NOT NULL DEFAULT 0,
    kept          INTEGER NOT NULL DEFAULT 0,
    error         TEXT    NOT NULL DEFAULT '',
    playlist_path TEXT    NOT NULL DEFAULT ''
);
CREATE TABLE suggestions (
    id             INTEGER PRIMARY KEY,
    scan_id        INTEGER REFERENCES scans (id) ON DELETE SET NULL,
    track_key      TEXT    NOT NULL UNIQUE,
    artist         TEXT    NOT NULL,
    title          TEXT    NOT NULL,
    album          TEXT    NOT NULL DEFAULT '',
    year           TEXT    NOT NULL DEFAULT '',
    track_no       INTEGER NOT NULL DEFAULT 0,
    match          INTEGER NOT NULL DEFAULT 0,
    reason         TEXT    NOT NULL DEFAULT '',
    seed           TEXT    NOT NULL DEFAULT '',
    tags           TEXT    NOT NULL DEFAULT '',
    cover_url      TEXT    NOT NULL DEFAULT '',
    duration       INTEGER NOT NULL DEFAULT 0,
    recording_mbid TEXT    NOT NULL DEFAULT '',
    status         TEXT    NOT NULL DEFAULT 'new',
    error          TEXT    NOT NULL DEFAULT '',
    created_at     INTEGER NOT NULL,
    decided_at     INTEGER
);
CREATE TABLE downloads (
    id            INTEGER PRIMARY KEY,
    suggestion_id INTEGER REFERENCES suggestions (id) ON DELETE SET NULL,
    track_key     TEXT    NOT NULL,
    artist        TEXT    NOT NULL,
    title         TEXT    NOT NULL,
    album         TEXT    NOT NULL DEFAULT '',
    path          TEXT    NOT NULL DEFAULT '',
    source_url    TEXT    NOT NULL DEFAULT '',
    source        TEXT    NOT NULL DEFAULT '',
    bytes         INTEGER NOT NULL DEFAULT 0,
    duration      INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'queued',
    progress      INTEGER NOT NULL DEFAULT 0,
    error         TEXT    NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL,
    finished_at   INTEGER
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE stats_cache (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE excluded_files (
    path TEXT PRIMARY KEY, mtime REAL NOT NULL,
    track_key TEXT NOT NULL, artist_key TEXT NOT NULL
);
CREATE TABLE sync_state (
    source TEXT PRIMARY KEY, cursor INTEGER NOT NULL DEFAULT 0,
    synced_at INTEGER, error TEXT NOT NULL DEFAULT ''
);
"""


@pytest.fixture
def legacy_database():
    """Replace the fresh database with a populated single-user one."""
    for suffix in ("", "-wal", "-shm"):
        (config.DB_PATH.parent / (config.DB_PATH.name + suffix)).unlink(missing_ok=True)

    conn = sqlite3.connect(config.DB_PATH)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO plays (artist, title, artist_key, track_key, played_at, source) "
        "VALUES ('Radiohead', 'Karma Police', 'radiohead', 'radiohead|karmapolice', 1700, 'lastfm')"
    )
    conn.execute(
        "INSERT INTO scans (started_at, status) VALUES (1700, 'ok')"
    )
    conn.execute(
        "INSERT INTO suggestions (track_key, artist, title, status, match, created_at) "
        "VALUES ('portishead|glorybox', 'Portishead', 'Glory Box', 'saved', 88, 1700)"
    )
    conn.execute(
        "INSERT INTO downloads (track_key, artist, title, status, path, created_at) "
        "VALUES ('portishead|glorybox', 'Portishead', 'Glory Box', 'done', '/music/x.mp3', 1700)"
    )
    conn.execute("INSERT INTO sync_state (source, cursor) VALUES ('lastfm', 1700)")
    conn.commit()
    conn.close()
    yield


def test_the_upgrade_keeps_every_row(legacy_database):
    db.init()

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM plays").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM suggestions").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM downloads").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM scans").fetchone()["n"] == 1


def test_the_upgrade_preserves_decisions(legacy_database):
    """A saved card must still be saved afterwards, not reset to new."""
    db.init()

    with db.connect() as conn:
        row = conn.execute("SELECT status, match, artist FROM suggestions").fetchone()
    assert row["status"] == "saved"
    assert row["match"] == 88
    assert row["artist"] == "Portishead"


def test_everything_lands_on_the_first_user(legacy_database):
    db.init()
    user_id = users.default_id()
    assert user_id is not None

    with db.connect() as conn:
        for table in ("plays", "suggestions", "downloads", "scans"):
            owners = {
                row["user_id"] for row in conn.execute(f"SELECT user_id FROM {table}")
            }
            assert owners == {user_id}, f"{table} was not assigned to the first user"


def test_the_sync_cursor_survives(legacy_database):
    db.init()

    with db.connect() as conn:
        row = conn.execute(
            "SELECT cursor FROM user_sync_state WHERE source = 'lastfm'"
        ).fetchone()
    assert row is not None and row["cursor"] == 1700


def test_the_new_unique_constraints_are_in_place(legacy_database):
    """The whole point of the rebuild: per-user uniqueness, not global."""
    db.init()
    user_id = users.default_id()
    other = users.create(name="alex")["id"]

    with db.connect() as conn:
        # The same track for a different user is now allowed...
        conn.execute(
            "INSERT INTO suggestions (user_id, track_key, artist, title, created_at) "
            "VALUES (?, 'portishead|glorybox', 'Portishead', 'Glory Box', 1700)",
            (other,),
        )
        # ...and a duplicate for the same user is still refused.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO suggestions (user_id, track_key, artist, title, created_at) "
                "VALUES (?, 'portishead|glorybox', 'Portishead', 'Glory Box', 1700)",
                (user_id,),
            )


def test_the_upgrade_is_idempotent(legacy_database):
    """Boot loops and re-runs must not duplicate rows or re-migrate."""
    db.init()
    db.init()
    db.init()

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM plays").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 1
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == db.SCHEMA_VERSION


def test_no_leftover_scratch_tables(legacy_database):
    db.init()

    with db.connect() as conn:
        names = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert not any(name.endswith("_pre_multiuser") for name in names)
