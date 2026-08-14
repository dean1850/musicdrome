"""Taking a multi-user database back to one listener.

Musicdrome was briefly a household app: a users table, and a ``user_id`` on
every row. Undoing that is the one change here that can destroy data rather
than merely misbehave, so these tests build databases in the *old* shapes, with
rows in them, and pin down exactly what survives.

Two shapes matter, and they are treated very differently:

* **v2, multi-user.** History and suggestions are dropped and re-synced —
  collapsing two people's rows into one profile would mean picking whose
  decisions win. Downloads are kept, because those rows name files that are on
  the disk right now.
* **v1, the original single-listener schema.** Nothing to undo, so nothing is
  touched. An install that never ran a multi-user image must not lose a thing.

Both schemas are written out in full rather than imported, because the point is
to test against what is actually on disk in an existing install — definitions
that have by now been edited out of the source.
"""

import sqlite3

import pytest

from app import config, db

V1_SCHEMA = """
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

V2_SCHEMA = """
CREATE TABLE users (
    id                 INTEGER PRIMARY KEY,
    name               TEXT    NOT NULL UNIQUE,
    email              TEXT    NOT NULL DEFAULT '',
    lastfm_user        TEXT    NOT NULL DEFAULT '',
    listenbrainz_user  TEXT    NOT NULL DEFAULT '',
    listenbrainz_token TEXT    NOT NULL DEFAULT '',
    source             TEXT    NOT NULL DEFAULT 'manual',
    active             INTEGER NOT NULL DEFAULT 1,
    created_at         INTEGER NOT NULL
);
CREATE TABLE plays (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER REFERENCES users (id) ON DELETE CASCADE,
    artist      TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    album       TEXT    NOT NULL DEFAULT '',
    artist_key  TEXT    NOT NULL,
    track_key   TEXT    NOT NULL,
    played_at   INTEGER NOT NULL,
    source      TEXT    NOT NULL,
    UNIQUE (user_id, track_key, played_at, source)
);
CREATE TABLE scans (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER REFERENCES users (id) ON DELETE CASCADE,
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
    user_id        INTEGER REFERENCES users (id) ON DELETE CASCADE,
    track_key      TEXT    NOT NULL,
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
    decided_at     INTEGER,
    UNIQUE (user_id, track_key)
);
CREATE TABLE downloads (
    id            INTEGER PRIMARY KEY,
    suggestion_id INTEGER REFERENCES suggestions (id) ON DELETE SET NULL,
    user_id       INTEGER REFERENCES users (id) ON DELETE SET NULL,
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
CREATE TABLE user_sync_state (
    user_id   INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    source    TEXT    NOT NULL,
    cursor    INTEGER NOT NULL DEFAULT 0,
    synced_at INTEGER,
    error     TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, source)
);
PRAGMA user_version = 2;
"""


def _replace_database(script: str) -> sqlite3.Connection:
    for suffix in ("", "-wal", "-shm"):
        (config.DB_PATH.parent / (config.DB_PATH.name + suffix)).unlink(missing_ok=True)
    config.DB_PATH.with_suffix(".db.pre-single-user").unlink(missing_ok=True)

    conn = sqlite3.connect(config.DB_PATH)
    conn.executescript(script)
    return conn


@pytest.fixture
def multi_user_database():
    """A populated two-listener database, as a v2 install has on disk."""
    conn = _replace_database(V2_SCHEMA)
    conn.execute("INSERT INTO users (id, name, created_at) VALUES (1, 'dean', 1700)")
    conn.execute("INSERT INTO users (id, name, created_at) VALUES (2, 'alex', 1700)")
    conn.execute(
        "INSERT INTO plays (user_id, artist, title, artist_key, track_key, played_at, source) "
        "VALUES (1, 'Radiohead', 'Karma Police', 'radiohead', 'radiohead|karmapolice', 1700, 'lastfm')"
    )
    conn.execute("INSERT INTO scans (id, user_id, started_at, status) VALUES (5, 1, 1700, 'ok')")
    conn.execute(
        "INSERT INTO suggestions (id, user_id, scan_id, track_key, artist, title, status, "
        "match, created_at) VALUES (9, 1, 5, 'portishead|glorybox', 'Portishead', "
        "'Glory Box', 'downloaded', 88, 1700)"
    )
    conn.execute(
        "INSERT INTO downloads (suggestion_id, user_id, track_key, artist, title, status, "
        "path, bytes, created_at) VALUES (9, 1, 'portishead|glorybox', 'Portishead', "
        "'Glory Box', 'done', '/music/Portishead/Dummy/Glory Box.mp3', 4096, 1700)"
    )
    conn.execute("INSERT INTO user_sync_state (user_id, source, cursor) VALUES (1, 'lastfm', 1700)")
    conn.commit()
    conn.close()
    yield


@pytest.fixture
def original_database():
    """A populated database from before multi-user ever shipped."""
    conn = _replace_database(V1_SCHEMA)
    conn.execute(
        "INSERT INTO plays (artist, title, artist_key, track_key, played_at, source) "
        "VALUES ('Radiohead', 'Karma Police', 'radiohead', 'radiohead|karmapolice', 1700, 'lastfm')"
    )
    conn.execute(
        "INSERT INTO suggestions (track_key, artist, title, status, match, created_at) "
        "VALUES ('portishead|glorybox', 'Portishead', 'Glory Box', 'saved', 88, 1700)"
    )
    conn.execute("INSERT INTO sync_state (source, cursor) VALUES ('lastfm', 1700)")
    conn.commit()
    conn.close()
    yield


# ─── Coming back from multi-user ───────────────────────────────────────────


def test_the_users_table_is_gone(multi_user_database):
    db.init()

    with db.connect() as conn:
        names = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert "users" not in names
    assert "user_sync_state" not in names


def test_no_table_still_carries_a_user_column(multi_user_database):
    db.init()

    with db.connect() as conn:
        for table in ("plays", "suggestions", "scans", "downloads"):
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert "user_id" not in columns, f"{table} still has a user_id"


def test_downloads_survive_because_their_files_do(multi_user_database):
    """The rows name files on disk; dropping them would re-download the lot."""
    db.init()

    with db.connect() as conn:
        row = conn.execute("SELECT * FROM downloads").fetchone()
    assert row["artist"] == "Portishead"
    assert row["path"] == "/music/Portishead/Dummy/Glory Box.mp3"
    assert row["status"] == "done"
    assert row["bytes"] == 4096
    # The suggestion it pointed at has been dropped, so the link goes with it.
    assert row["suggestion_id"] is None


def test_history_is_dropped_to_be_re_synced(multi_user_database):
    db.init()

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM plays").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM suggestions").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM scans").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM sync_state").fetchone()["n"] == 0


def test_the_old_database_is_kept_as_a_backup(multi_user_database):
    db.init()

    backup = config.DB_PATH.with_suffix(".db.pre-single-user")
    assert backup.exists()

    # And it is still the multi-user one, so a rollback has something to use.
    conn = sqlite3.connect(backup)
    try:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "users" in names


def test_the_backup_is_not_overwritten_by_a_later_boot(multi_user_database):
    db.init()
    backup = config.DB_PATH.with_suffix(".db.pre-single-user")
    stamp = backup.stat().st_mtime_ns

    db.init()
    assert backup.stat().st_mtime_ns == stamp


def test_the_migration_is_idempotent(multi_user_database):
    """Boot loops and re-runs must not duplicate rows or re-migrate."""
    db.init()
    db.init()
    db.init()

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM downloads").fetchone()["n"] == 1
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == db.SCHEMA_VERSION


def test_no_leftover_scratch_tables(multi_user_database):
    db.init()

    with db.connect() as conn:
        names = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert not any("multiuser" in name for name in names)


def test_the_single_track_key_constraint_is_back(multi_user_database):
    db.init()

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO suggestions (track_key, artist, title, created_at) "
            "VALUES ('portishead|glorybox', 'Portishead', 'Glory Box', 1700)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO suggestions (track_key, artist, title, created_at) "
                "VALUES ('portishead|glorybox', 'Portishead', 'Glory Box', 1700)"
            )


# ─── Never having been multi-user ──────────────────────────────────────────


def test_an_original_database_loses_nothing(original_database):
    """It was already single-listener. There is nothing to undo."""
    db.init()

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM plays").fetchone()["n"] == 1
        row = conn.execute("SELECT status, match FROM suggestions").fetchone()
        cursor = conn.execute(
            "SELECT cursor FROM sync_state WHERE source = 'lastfm'"
        ).fetchone()
    assert row["status"] == "saved" and row["match"] == 88
    assert cursor["cursor"] == 1700


def test_an_older_database_gains_the_provenance_columns(original_database):
    """Added by ALTER on an existing downloads table, not only by CREATE on a
    fresh one — otherwise every upgraded install fails on the first download."""
    db.init()

    with db.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(downloads)")}
    assert {"source_codec", "source_abr", "encoded"} <= columns


def test_downloads_that_predate_the_columns_claim_nothing(original_database):
    """A file already on disk cannot say what it used to be, so its row stays
    blank rather than being backfilled with a guess."""
    db.init()

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO downloads (track_key, artist, title, status, path, created_at) "
            "VALUES ('a|one', 'A', 'One', 'done', '/music/A/Album/One.mp3', 1700)"
        )
        row = conn.execute("SELECT source_codec, source_abr, encoded FROM downloads").fetchone()

    assert (row["source_codec"], row["source_abr"], row["encoded"]) == ("", 0, "")


def test_an_original_database_is_not_backed_up(original_database):
    """Nothing destructive runs, so there is nothing to insure against."""
    db.init()
    assert not config.DB_PATH.with_suffix(".db.pre-single-user").exists()
