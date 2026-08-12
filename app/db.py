"""SQLite storage.

One file, WAL mode, plain ``sqlite3`` — no ORM and no database server. The
schema is created on first boot and migrated forward by :func:`_migrate`, which
only ever adds things, so a container can be rolled back without losing data.

Connections are opened per use rather than pooled. SQLite in WAL mode handles
concurrent readers alongside a single writer, which is exactly the shape of this
app: a background scan writing while the UI polls.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from . import config

log = logging.getLogger(__name__)

SCHEMA_TABLES = """
-- One row per person in the household. Musicdrome has no login: a user here is
-- a taste profile, not an account, and the UI simply asks which one you are.
--
-- Each carries its own scrobble identities, because that is the whole point —
-- two people on one server keep separate history, separate suggestions and
-- separate keep/hide decisions while sharing one music library on disk.
CREATE TABLE IF NOT EXISTS users (
    id                 INTEGER PRIMARY KEY,
    name               TEXT    NOT NULL UNIQUE,
    email              TEXT    NOT NULL DEFAULT '',
    lastfm_user        TEXT    NOT NULL DEFAULT '',
    listenbrainz_user  TEXT    NOT NULL DEFAULT '',
    listenbrainz_token TEXT    NOT NULL DEFAULT '',
    -- 'manual' or 'navidrome': where the row came from, so a re-import can
    -- refresh what Navidrome owns without touching hand-made entries.
    source             TEXT    NOT NULL DEFAULT 'manual',
    active             INTEGER NOT NULL DEFAULT 1,
    created_at         INTEGER NOT NULL
);

-- Scrobbles pulled from Last.fm and ListenBrainz.
CREATE TABLE IF NOT EXISTS plays (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER REFERENCES users (id) ON DELETE CASCADE,
    artist      TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    album       TEXT    NOT NULL DEFAULT '',
    artist_key  TEXT    NOT NULL,
    track_key   TEXT    NOT NULL,
    played_at   INTEGER NOT NULL,
    source      TEXT    NOT NULL,
    -- Scoped by user: two people playing the same track at the same second is
    -- two plays, and without user_id here one of them would be discarded by
    -- the INSERT OR IGNORE that absorbs re-read pages.
    UNIQUE (user_id, track_key, played_at, source)
);

-- One row per discovery run.
CREATE TABLE IF NOT EXISTS scans (
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

-- Recommendations. One row per track ever suggested: a re-suggestion refreshes
-- the existing row so a hidden card can never come back.
CREATE TABLE IF NOT EXISTS suggestions (
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
    -- Per user, not global. The same track can legitimately be suggested to
    -- two people, and each keeps their own status for it — one person hiding
    -- a card must never remove it from somebody else's grid.
    UNIQUE (user_id, track_key)
);

-- Files Musicdrome fetched, and the ones it failed to.
CREATE TABLE IF NOT EXISTS downloads (
    id            INTEGER PRIMARY KEY,
    suggestion_id INTEGER REFERENCES suggestions (id) ON DELETE SET NULL,
    -- Whose taste asked for this. The file itself is shared: one library on
    -- disk, so nothing here changes where it is written.
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

-- Runtime preferences, editable in the UI without a restart.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Cached AI output that would be wasteful to regenerate on every page load.
CREATE TABLE IF NOT EXISTS stats_cache (
    key        TEXT PRIMARY KEY,
    value      TEXT    NOT NULL,
    created_at INTEGER NOT NULL
);

-- Artist/title pairs read out of EXCLUDE_MUSIC_DIR, cached by mtime.
CREATE TABLE IF NOT EXISTS excluded_files (
    path       TEXT PRIMARY KEY,
    mtime      REAL    NOT NULL,
    track_key  TEXT    NOT NULL,
    artist_key TEXT    NOT NULL
);

-- How far each history source has been read. Superseded by user_sync_state
-- and kept only so a rollback to an older image still finds its table.
CREATE TABLE IF NOT EXISTS sync_state (
    source    TEXT PRIMARY KEY,
    cursor    INTEGER NOT NULL DEFAULT 0,
    synced_at INTEGER,
    error     TEXT    NOT NULL DEFAULT ''
);

-- How far each source has been read, per user. One person's Last.fm being
-- unreachable must not stall anybody else's sync, so the cursor and the last
-- error are both per (user, source).
CREATE TABLE IF NOT EXISTS user_sync_state (
    user_id   INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    source    TEXT    NOT NULL,
    cursor    INTEGER NOT NULL DEFAULT 0,
    synced_at INTEGER,
    error     TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, source)
);
"""

# Kept apart from the tables because they are created *after* the migration
# below. An index naming a column that an old table has not been given yet
# fails outright, which is how creating the schema and upgrading it in the
# same breath used to abort the boot of any pre-multi-user database.
SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS plays_played_at ON plays (played_at DESC);
CREATE INDEX IF NOT EXISTS plays_track_key ON plays (track_key);
CREATE INDEX IF NOT EXISTS plays_artist_key ON plays (artist_key);
CREATE INDEX IF NOT EXISTS suggestions_status ON suggestions (status, match DESC);
CREATE INDEX IF NOT EXISTS suggestions_scan ON suggestions (scan_id);
CREATE INDEX IF NOT EXISTS suggestions_user ON suggestions (user_id, status);
CREATE INDEX IF NOT EXISTS downloads_status ON downloads (status, created_at DESC);
CREATE INDEX IF NOT EXISTS downloads_track_key ON downloads (track_key);
CREATE INDEX IF NOT EXISTS excluded_track_key ON excluded_files (track_key);
"""

SCHEMA = SCHEMA_TABLES + SCHEMA_INDEXES

# Bumped when a migration beyond CREATE-IF-NOT-EXISTS is needed. Stored in
# SQLite's own `user_version`, so it costs no table and no query.
#   1 → single-user (anything built before multi-user support)
#   2 → per-user plays, suggestions, scans, downloads and sync cursors
SCHEMA_VERSION = 2

# Runtime preferences and their defaults. Anything not listed here is not a
# setting — it is either a startup concern (config.py) or a hardcoded decision.
DEFAULT_SETTINGS: dict[str, Any] = {
    "schedule": "daily",             # off | 6h | daily | weekly
    "batch_size": 40,                # tracks requested per AI call (5-100)
    "history_days": 90,              # listening window the taste profile covers
    "min_match": 0,                  # hide cards below this % in the UI
    "auto_download": False,
    "auto_download_threshold": 85,
    "daily_download_cap": 25,
    "retention_days": 60,            # purge un-actioned suggestions after this
    "taste_summary": True,           # AI-written paragraph on the stats page
    "sort": "match",                 # match | newest | artist
}


def now() -> int:
    return int(time.time())


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """A connection that commits on success and rolls back on failure."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Forward-only migrations for databases created by an earlier version.

    Adding a column here is safe to run repeatedly; the ``OperationalError`` for
    a column that already exists is the expected path on every boot after the
    first.
    """
    additions = {
        "scans": [("trigger", "TEXT NOT NULL DEFAULT 'manual'")],
        "suggestions": [
            ("recording_mbid", "TEXT NOT NULL DEFAULT ''"),
            ("track_no", "INTEGER NOT NULL DEFAULT 0"),
        ],
        "downloads": [("progress", "INTEGER NOT NULL DEFAULT 0")],
    }
    for table, columns in additions.items():
        for name, definition in columns:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            except sqlite3.OperationalError:
                pass


def _table_ddl(table: str) -> str:
    """The CREATE TABLE statement for one table, taken from the schema above.

    Rebuilding a table during a migration needs its new definition, and reading
    it back out of the single schema string keeps there from being a second
    copy that can drift out of step with the first.
    """
    for statement in SCHEMA_TABLES.split(";"):
        if f"CREATE TABLE IF NOT EXISTS {table} (" in statement.replace("\n", " "):
            return statement + ";"
    raise KeyError(f"no DDL for {table} in SCHEMA_TABLES")


def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return (row["sql"] or "") if row else ""


def _default_user_id(conn: sqlite3.Connection) -> int:
    """The user that inherits everything a single-user database already had.

    Named after whichever scrobble account the environment already carries, so
    an upgraded install shows the name its owner would expect rather than a
    placeholder.
    """
    row = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    if row:
        return int(row["id"])

    name = config.LASTFM_USER or config.LISTENBRAINZ_USER or "default"
    cursor = conn.execute(
        "INSERT INTO users (name, lastfm_user, listenbrainz_user, listenbrainz_token, "
        "source, active, created_at) VALUES (?, ?, ?, ?, 'manual', 1, ?)",
        (
            name,
            config.LASTFM_USER,
            config.LISTENBRAINZ_USER,
            config.LISTENBRAINZ_TOKEN,
            now(),
        ),
    )
    log.info("created the first user '%s' from the environment", name)
    return int(cursor.lastrowid)


def _upgrade_to_multi_user() -> None:
    """Give an existing single-user database a user column, and one user.

    Two of these tables need their UNIQUE constraint changed, which SQLite can
    only do by rebuilding the table. That means foreign keys have to come off
    for the duration — a rename would otherwise drag child rows with it — so
    this runs on its own connection with explicit transaction control rather
    than through :func:`connect`.

    Every step is conditional, so this is safe to run against a database that
    is already current, and safe to re-run after an interrupted boot.
    """
    conn = sqlite3.connect(config.DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # we manage BEGIN/COMMIT ourselves
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")

        user_id = _default_user_id(conn)

        # Tables that only gain a column keep their data in place.
        for table, on_delete in (("scans", "CASCADE"), ("downloads", "SET NULL")):
            if "user_id" in _table_sql(conn, table):
                continue
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN user_id INTEGER "
                f"REFERENCES users (id) ON DELETE {on_delete}"
            )
            conn.execute(f"UPDATE {table} SET user_id = ?", (user_id,))
            log.info("migrated %s to per-user rows", table)

        # Tables whose UNIQUE constraint has to change are rebuilt. Detected by
        # the absence of user_id from the stored CREATE statement, which is
        # only true of a table this migration has not touched.
        for table in ("plays", "suggestions"):
            if "user_id" in _table_sql(conn, table):
                continue
            columns = [
                row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
            ]
            joined = ", ".join(columns)
            conn.execute(f"ALTER TABLE {table} RENAME TO {table}_pre_multiuser")
            # execute, not executescript: executescript commits first, which
            # would end the transaction this migration depends on.
            conn.execute(_table_ddl(table))  # recreate it in its new shape
            conn.execute(
                f"INSERT INTO {table} (user_id, {joined}) "
                f"SELECT ?, {joined} FROM {table}_pre_multiuser",
                (user_id,),
            )
            conn.execute(f"DROP TABLE {table}_pre_multiuser")
            log.info("rebuilt %s with a per-user unique constraint", table)

        # Cursors move to the per-user table so each person syncs independently.
        for row in conn.execute("SELECT * FROM sync_state"):
            conn.execute(
                "INSERT INTO user_sync_state (user_id, source, cursor, synced_at, error) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (user_id, source) DO NOTHING",
                (user_id, row["source"], row["cursor"], row["synced_at"], row["error"]),
            )

        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except Exception:
        # Best-effort: if the failure was one that already ended the
        # transaction, rolling back raises too and would replace the real
        # error with a misleading one.
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.close()


def init() -> None:
    """Create or migrate the database. Safe to call on every boot.

    Order matters. Tables first — which creates anything missing and leaves
    existing tables alone — then the column and constraint migrations, and only
    then the indexes, because an index over ``user_id`` cannot be built until
    every table actually has that column.
    """
    config.ensure_directories()
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA_TABLES)
        _migrate(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]

    if version < SCHEMA_VERSION:
        _upgrade_to_multi_user()

    with connect() as conn:
        conn.executescript(SCHEMA_INDEXES)

    log.info("database ready at %s", config.DB_PATH)


# ─── Settings ──────────────────────────────────────────────────────────────


def get_settings() -> dict[str, Any]:
    """Runtime settings, with any unset key falling back to its default."""
    values = dict(DEFAULT_SETTINGS)
    with connect() as conn:
        for row in conn.execute("SELECT key, value FROM settings"):
            if row["key"] not in DEFAULT_SETTINGS:
                continue  # a setting this version no longer has
            try:
                values[row["key"]] = json.loads(row["value"])
            except ValueError:
                pass
    return values


def get_setting(key: str) -> Any:
    return get_settings().get(key, DEFAULT_SETTINGS.get(key))


def save_settings(updates: dict[str, Any]) -> dict[str, Any]:
    """Persist known settings, coercing each to the type of its default."""
    clean: dict[str, Any] = {}
    for key, value in updates.items():
        if key not in DEFAULT_SETTINGS:
            continue
        default = DEFAULT_SETTINGS[key]
        try:
            if isinstance(default, bool):
                value = value if isinstance(value, bool) else str(value).lower() in {"1", "true", "yes", "on"}
            elif isinstance(default, int):
                value = int(value)
            else:
                value = str(value)
        except (TypeError, ValueError):
            continue
        clean[key] = value

    clean = _clamp(clean)
    with connect() as conn:
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [(key, json.dumps(value)) for key, value in clean.items()],
        )
    return get_settings()


def _clamp(values: dict[str, Any]) -> dict[str, Any]:
    """Keep numeric settings inside ranges the rest of the app can rely on."""
    bounds = {
        "batch_size": (5, 100),
        "history_days": (7, 3650),
        "min_match": (0, 100),
        "auto_download_threshold": (0, 100),
        "daily_download_cap": (0, 500),
        "retention_days": (1, 3650),
    }
    for key, (low, high) in bounds.items():
        if key in values:
            values[key] = max(low, min(high, values[key]))
    if "schedule" in values and values["schedule"] not in {"off", "6h", "daily", "weekly"}:
        values["schedule"] = "daily"
    if "sort" in values and values["sort"] not in {"match", "newest", "artist"}:
        values["sort"] = "match"
    return values


# ─── Cache ─────────────────────────────────────────────────────────────────


def cache_get(key: str, max_age: int) -> Any | None:
    """A cached value, or ``None`` if it is missing or older than ``max_age``."""
    with connect() as conn:
        row = conn.execute(
            "SELECT value, created_at FROM stats_cache WHERE key = ?", (key,)
        ).fetchone()
    if row is None or now() - row["created_at"] > max_age:
        return None
    try:
        return json.loads(row["value"])
    except ValueError:
        return None


def cache_put(key: str, value: Any) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO stats_cache (key, value, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, created_at = excluded.created_at",
            (key, json.dumps(value), now()),
        )
