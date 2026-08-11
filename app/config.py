"""Runtime configuration.

Everything here comes from the environment, and therefore from ``.env``. Every
value has a working default — a ``.env`` carrying only your Last.fm key and one
AI credential is a complete configuration.

Only startup concerns live here: credentials, usernames, paths, the AI backend.
Anything you would want to change without restarting the container (scan
schedule, batch size, auto-download threshold, retention) is a runtime setting
stored in SQLite instead — see :mod:`app.db`.

Container deployments set the ``MUSICDROME_*_DIR`` names to in-container paths;
a bare-metal or test run falls back to the host-side ``*_DIR`` spelling, so both
work without duplicating the file.
"""

from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _env(*names: str, default: str = "") -> str:
    """First non-empty value among ``names``, else ``default``."""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, default=str(default)))
    except ValueError:
        return default


def _path(*names: str, default: Path) -> Path:
    raw = _env(*names)
    return Path(raw).expanduser() if raw else default


# ─── Server ────────────────────────────────────────────────────────────────

PORT = _int("MUSICDROME_PORT", 3046)
HOST = _env("MUSICDROME_HOST", default="0.0.0.0")
LOG_LEVEL = _env("MUSICDROME_LOG_LEVEL", default="info").lower()
TIMEZONE = _env("TZ", default="UTC")

# ─── Paths ─────────────────────────────────────────────────────────────────

MUSIC_DIR = _path("MUSICDROME_MUSIC_DIR", "MUSIC_DIR", default=_REPO_ROOT / "data" / "music")
DATA_DIR = _path("MUSICDROME_DATA_DIR", "DATA_DIR", default=_REPO_ROOT / "data" / "config")
STATIC_DIR = _path("MUSICDROME_STATIC_DIR", default=Path(__file__).resolve().parent / "static")

# Optional read-only library scanned for artist/title only, purely so we never
# suggest something you already own. No database of it is kept.
EXCLUDE_MUSIC_DIR = _env("EXCLUDE_MUSIC_DIR")

DB_PATH = DATA_DIR / "musicdrome.db"
PLAYLIST_DIR = MUSIC_DIR / "_playlists"

# ─── Listening history ─────────────────────────────────────────────────────

LASTFM_API_KEY = _env("LASTFM_API_KEY")
LASTFM_USER = _env("LASTFM_USER")

LISTENBRAINZ_USER = _env("LISTENBRAINZ_USER")
LISTENBRAINZ_TOKEN = _env("LISTENBRAINZ_TOKEN")
LISTENBRAINZ_API_URL = _env("LISTENBRAINZ_API_URL", default="https://api.listenbrainz.org")

MUSICBRAINZ_API_URL = _env("MUSICBRAINZ_API_URL", default="https://musicbrainz.org/ws/2")
MUSICBRAINZ_USER_AGENT = _env(
    "MUSICBRAINZ_USER_AGENT",
    default="Musicdrome/2.0 ( https://github.com/dean1850/musicdrome )",
)

# ─── AI ────────────────────────────────────────────────────────────────────

AI_PROVIDER = _env("AI_PROVIDER", default="ollama").lower()

ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = _env("ANTHROPIC_MODEL", default="claude-sonnet-4-5")
ANTHROPIC_BASE_URL = _env("ANTHROPIC_BASE_URL", default="https://api.anthropic.com")

OPENAI_API_KEY = _env("OPENAI_API_KEY")
OPENAI_MODEL = _env("OPENAI_MODEL", default="gpt-4o-mini")
OPENAI_BASE_URL = _env("OPENAI_BASE_URL", default="https://api.openai.com/v1")

OLLAMA_BASE_URL = _env("OLLAMA_BASE_URL", default="http://host.docker.internal:11434")
OLLAMA_MODEL = _env("OLLAMA_MODEL", default="llama3.1")

AI_MAX_TOKENS = _int("AI_MAX_TOKENS", 8192)
AI_REQUEST_TIMEOUT = _int("AI_REQUEST_TIMEOUT", 300)

# ─── Downloads ─────────────────────────────────────────────────────────────

# Format is deliberately not configurable: MP3 at 320 kbps, always.
AUDIO_BITRATE = "320"
FFMPEG_PATH = _env("FFMPEG_PATH", default="/usr/bin/ffmpeg")

# Escape hatches for networks and rate limits, not everyday settings.
YTDLP_COOKIES_FILE = _env("YTDLP_COOKIES_FILE")
YTDLP_PROXY = _env("YTDLP_PROXY")
YTDLP_RATE_LIMIT = _env("YTDLP_RATE_LIMIT")
DOWNLOAD_CONCURRENCY = _int("DOWNLOAD_CONCURRENCY", 2)

TESTING = _env("MUSICDROME_TESTING").lower() in {"1", "true", "yes"}


def ensure_directories() -> None:
    """Create the directories we own. MUSIC_DIR may be a read-only mount."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for path in (MUSIC_DIR, PLAYLIST_DIR):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # read-only mount — downloads will report the failure


def history_sources() -> list[str]:
    """Which scrobble services are configured well enough to poll."""
    sources = []
    if LASTFM_API_KEY and LASTFM_USER:
        sources.append("lastfm")
    if LISTENBRAINZ_USER:
        sources.append("listenbrainz")
    return sources
