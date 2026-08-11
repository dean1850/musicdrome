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

SCHEMA = """
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
    UNIQUE (track_key, played_at, source)
);
CREATE INDEX IF NOT EXISTS plays_played_at ON plays (played_at DESC);
CREATE INDEX IF NOT EXISTS plays_track_key ON plays (track_key);
CREATE INDEX IF NOT EXISTS plays_artist_key ON plays (artist_key);

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
    error         TEXT    NOT NULL DEFAULT '',
    playlist_path TEXT    NOT NULL DEFAULT ''
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
CREATE INDEX IF NOT EXISTS suggestions_status ON suggestions (status, match DESC);
CREATE INDEX IF NOT EXISTS suggestions_scan ON suggestions (scan_id);

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
CREATE INDEX IF NOT EXISTS downloads_status ON downloads (status, created_at DESC);
CREATE INDEX IF NOT EXISTS downloads_track_key ON downloads (track_key);

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
CREATE INDEX IF NOT EXISTS excluded_track_key ON excluded_files (track_key);

-- How far each history source has been read.
CREATE TABLE IF NOT EXISTS sync_state (
    source    TEXT PRIMARY KEY,
    cursor    INTEGER NOT NULL DEFAULT 0,
    synced_at INTEGER,
    error     TEXT    NOT NULL DEFAULT ''
);
"""

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


def init() -> None:
    """Create or migrate the database. Safe to call on every boot."""
    config.ensure_directories()
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
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
