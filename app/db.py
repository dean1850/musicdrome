"""SQLite storage.

One file, WAL mode, plain ``sqlite3`` — no ORM and no database server. The
schema is created on first boot and migrated forward by :func:`_migrate`, which
only ever adds things, so a container can be rolled back without losing data.

Connections are opened per use rather than pooled. SQLite in WAL mode handles
concurrent readers alongside a single writer, which is exactly the shape of this
app: a background scan writing while the UI polls.

Musicdrome is single-listener: one Last.fm account, one ListenBrainz account,
one taste profile, taken from the environment. There is no users table and
nothing here is scoped by person.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from . import config

log = logging.getLogger(__name__)

SCHEMA_TABLES = """
-- Scrobbles pulled from Last.fm and ListenBrainz.
CREATE TABLE IF NOT EXISTS plays (
    id          INTEGER PRIMARY KEY,
    artist      TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    album       TEXT    NOT NULL DEFAULT '',
    artist_key  TEXT    NOT NULL,
    track_key   TEXT    NOT NULL,
    played_at   INTEGER NOT NULL,
    source      TEXT    NOT NULL,
    -- Absorbs the overlap when a sync re-reads a page it has already stored.
    UNIQUE (track_key, played_at, source)
);

-- One row per discovery run.
CREATE TABLE IF NOT EXISTS scans (
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
    error         TEXT    NOT NULL DEFAULT ''
);

-- Recommendations. One row per track ever suggested: a re-suggestion refreshes
-- the existing row so a hidden card can never come back.
CREATE TABLE IF NOT EXISTS suggestions (
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

-- Files Musicdrome fetched, and the ones it failed to.
CREATE TABLE IF NOT EXISTS downloads (
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

-- How far each history source has been read: the timestamp of the newest play
-- stored, so a sync only ever asks for what it has not seen.
CREATE TABLE IF NOT EXISTS sync_state (
    source    TEXT PRIMARY KEY,
    cursor    INTEGER NOT NULL DEFAULT 0,
    synced_at INTEGER,
    error     TEXT    NOT NULL DEFAULT ''
);
"""

# Kept apart from the tables because they are created *after* the migration
# below. An index naming a column a table has not been given yet fails outright,
# which would abort the boot rather than the migration.
SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS plays_played_at ON plays (played_at DESC);
CREATE INDEX IF NOT EXISTS plays_track_key ON plays (track_key);
CREATE INDEX IF NOT EXISTS plays_artist_key ON plays (artist_key);
CREATE INDEX IF NOT EXISTS suggestions_status ON suggestions (status, match DESC);
CREATE INDEX IF NOT EXISTS suggestions_scan ON suggestions (scan_id);
CREATE INDEX IF NOT EXISTS downloads_status ON downloads (status, created_at DESC);
CREATE INDEX IF NOT EXISTS downloads_track_key ON downloads (track_key);
CREATE INDEX IF NOT EXISTS excluded_track_key ON excluded_files (track_key);
"""

SCHEMA = SCHEMA_TABLES + SCHEMA_INDEXES

# Bumped when a migration beyond CREATE-IF-NOT-EXISTS is needed. Stored in
# SQLite's own `user_version`, so it costs no table and no query.
#   1 → the original single-listener schema
#   2 → per-user plays, suggestions, scans, downloads and sync cursors
#   3 → single listener again; the users table and every user_id column removed
SCHEMA_VERSION = 3

# The tables :func:`_reset_to_single_user` clears, in the order it drops them.
# `downloads` is deliberately absent: those rows describe files that are on
# disk right now, and forgetting them would re-download the lot.
MULTI_USER_TABLES = (
    "plays",
    "suggestions",
    "scans",
    "user_sync_state",
    "sync_state",
    "users",
    # Left behind by an interrupted 1 → 2 migration.
    "plays_pre_multiuser",
    "suggestions_pre_multiuser",
)

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


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def _reset_to_single_user() -> None:
    """Take a multi-user database back to one listener.

    Musicdrome was briefly a household app, with a users table and a ``user_id``
    on every row. Undoing that cleanly is not a column drop: ``plays`` and
    ``suggestions`` had their UNIQUE constraints widened to include the user, so
    both have to be rebuilt, and collapsing two people's rows into one profile
    would mean choosing whose hide/save decisions survive.

    So the listening history is dropped and re-synced instead — Last.fm and
    ListenBrainz still hold every scrobble, and the first scan pulls them back.
    What is *not* dropped is ``downloads``: those rows point at files sitting in
    the library right now, and losing them would re-download every one.

    Runs on its own connection with foreign keys off and explicit transaction
    control, because dropping a parent table with children attached is exactly
    what foreign keys are there to prevent.
    """
    conn = sqlite3.connect(config.DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # we manage BEGIN/COMMIT ourselves
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")

        for table in MULTI_USER_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")

        # downloads keeps its rows and loses its user column, which means a
        # rebuild — SQLite cannot drop a column that a constraint mentions, and
        # doing it by hand keeps this working on older SQLite builds too.
        if _has_table(conn, "downloads"):
            carried = [name for name in _columns(conn, "downloads") if name != "user_id"]
            joined = ", ".join(carried)
            conn.execute("ALTER TABLE downloads RENAME TO downloads_multiuser")
            conn.execute(_table_ddl("downloads"))
            conn.execute(
                f"INSERT INTO downloads ({joined}) SELECT {joined} FROM downloads_multiuser"
            )
            conn.execute("DROP TABLE downloads_multiuser")
            kept = conn.execute("SELECT COUNT(*) AS n FROM downloads").fetchone()["n"]
            log.info("kept %d download records; the files they name are untouched", kept)

        # Every suggestion they pointed at has just been dropped.
        conn.execute("UPDATE downloads SET suggestion_id = NULL")

        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
        log.info("migrated to the single-listener schema — history will re-sync on the next scan")
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


def _backup_before_reset() -> None:
    """Copy the database aside before the multi-user tables are dropped.

    Cheap insurance, taken once and never overwritten: rolling back to a
    multi-user image is otherwise impossible after the migration has run.
    """
    backup = config.DB_PATH.with_suffix(".db.pre-single-user")
    if backup.exists() or not config.DB_PATH.exists():
        return
    try:
        # A WAL checkpoint first, so the copy carries committed pages rather
        # than a main file that is missing the newest ones.
        with connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(FULL)")
        shutil.copy2(config.DB_PATH, backup)
        log.info("saved the multi-user database as %s", backup.name)
    except OSError as exc:
        log.warning("could not back the database up before migrating: %s", exc)


def init() -> None:
    """Create or migrate the database. Safe to call on every boot.

    Order matters. The multi-user teardown runs *first*, because it drops
    tables that the schema below would otherwise recreate in their old shape;
    then the tables, then column migrations, and only then the indexes.
    """
    config.ensure_directories()

    # The users table is the tell. A pre-multi-user database never had one, and
    # neither does a fresh install, so its presence is what identifies the one
    # schema that needs tearing down.
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        multi_user = _has_table(conn, "users")

    if multi_user:
        _backup_before_reset()
        _reset_to_single_user()

    with connect() as conn:
        conn.executescript(SCHEMA_TABLES)
        _migrate(conn)
        conn.executescript(SCHEMA_INDEXES)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

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
