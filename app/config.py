"""Runtime configuration.

Everything here comes from the environment, and therefore from ``.env``. Every
value has a working default — a ``.env`` carrying only your Last.fm key and one
AI credential is a complete configuration.

Musicdrome listens to one person. The Last.fm and ListenBrainz names below are
that person, and there is nothing else to configure about who you are.

Only startup concerns live here: credentials, usernames, paths, the AI backend.
Anything you would want to change without restarting the container (scan
schedule, batch size, auto-download threshold, retention) is a runtime setting
stored in SQLite instead — see :mod:`app.db`.

Container deployments set the ``MUSICDROME_*_DIR`` names to in-container paths;
a bare-metal or test run falls back to the host-side ``*_DIR`` spelling, so both
work without duplicating the file.
"""

from __future__ import annotations

import errno
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

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
# One playlist, appended to for the life of the install. It used to be one file
# per scan, which turned a library server's playlist list into a wall of
# musicdrome-scan-0001, -0002, -0003 that nobody ever opened twice.
PLAYLIST_NAME = _env("MUSICDROME_PLAYLIST_NAME", default="Musicdrome") or "Musicdrome"
PLAYLIST_PATH = PLAYLIST_DIR / f"{PLAYLIST_NAME}.m3u"
# Scratch space for in-flight downloads. Kept out of DATA_DIR's top level so
# multi-megabyte partial audio never sits beside the database, and swept at
# boot in case a container was killed mid-download.
TMP_DIR = DATA_DIR / "tmp"

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
ANTHROPIC_MODEL = _env("ANTHROPIC_MODEL", default="claude-opus-5")
ANTHROPIC_BASE_URL = _env("ANTHROPIC_BASE_URL", default="https://api.anthropic.com")

OPENAI_API_KEY = _env("OPENAI_API_KEY")
OPENAI_MODEL = _env("OPENAI_MODEL", default="gpt-4o-mini")
OPENAI_BASE_URL = _env("OPENAI_BASE_URL", default="https://api.openai.com/v1")

OLLAMA_BASE_URL = _env("OLLAMA_BASE_URL", default="http://host.docker.internal:11434")
OLLAMA_MODEL = _env("OLLAMA_MODEL", default="llama3.1")

AI_MAX_TOKENS = _int("AI_MAX_TOKENS", 8192)
AI_REQUEST_TIMEOUT = _int("AI_REQUEST_TIMEOUT", 300)

# ─── Downloads ─────────────────────────────────────────────────────────────

# MP3 at 320 kbps by default. Worth knowing what the tradeoff is before
# changing it: YouTube serves Opus at around 160 kbps, so "mp3"/"320" makes a
# file about twice the size that is fractionally *worse* than the source,
# bought in exchange for playing on absolutely everything. "opus" or "m4a"
# keeps the original bytes with no second encode.
AUDIO_FORMAT = _env("AUDIO_FORMAT", default="mp3").lower()
AUDIO_BITRATE = _env("AUDIO_BITRATE", default="320")
FFMPEG_PATH = _env("FFMPEG_PATH", default="/usr/bin/ffmpeg")

# Which YouTube player clients yt-dlp may use, in order. Empty is the right
# answer and the default: yt-dlp picks its own, and those defaults move every
# time YouTube changes something. Pinning a list here freezes that decision at
# the moment the list was written, which is how this setting previously ended
# up leading with `ios` and `android` — the two clients YouTube has since moved
# to SABR-only delivery, where the player response carries no format URLs at
# all. That is what fills the log with "Some android client https formats have
# been skipped as they are missing a URL" and yt-dlp/yt-dlp#12482.
#
# Set this only to work around a specific, current breakage, and clear it again
# afterwards. yt-dlp's own defaults track upstream; a pin here does not.
YTDLP_PLAYER_CLIENTS = _env("YTDLP_PLAYER_CLIENTS")

# The clients that do still carry format URLs need a JavaScript runtime to
# solve YouTube's signature and n-challenges, plus yt-dlp's external JS
# components ("EJS") which are fetched on demand rather than bundled. Without
# both, every remaining client is either SABR-only or unsolvable and downloads
# fail with messages that never mention the real cause.
#
# The image installs Deno for this, which is yt-dlp's own default and the one
# it recommends — it sandboxes the challenge scripts it runs.
#
# It used to install Debian's `nodejs` instead, on the grounds that Node was
# packaged and Deno was not. That was wrong in a way nothing reported: yt-dlp
# requires Node >= 22.0.0, Debian ships 18 on bookworm and 20 on trixie, and a
# runtime below the minimum is treated as absent. yt-dlp then fell back to its
# JS-less clients and YouTube answered those with HTTP 403 partway through the
# download. Whatever is named here has to satisfy yt-dlp's minimum: deno 2.3,
# node 22, quickjs 2023-12-9.
YTDLP_JS_RUNTIMES = _env("YTDLP_JS_RUNTIMES", default="deno")
YTDLP_REMOTE_COMPONENTS = _env("YTDLP_REMOTE_COMPONENTS", default="ejs:github")

# Which browser yt-dlp should impersonate at the TLS layer, via curl_cffi.
#
# YouTube fingerprints the connection itself — cipher order, ALPN, HTTP/2
# settings — and python's own stack looks nothing like a browser. On a
# residential address that is usually tolerated; from a datacenter or VPN exit
# it is the difference between a download and "HTTP Error 403: Forbidden"
# partway through the media fetch, after the search and the metadata call have
# both succeeded. Impersonation makes the handshake indistinguishable from
# Chrome's, which is the single most effective thing available against those
# 403s and costs nothing when they are not happening.
#
# Set to "" to disable. If curl_cffi is missing this is ignored rather than
# fatal — see :func:`app.download.impersonate_target`.
YTDLP_IMPERSONATE = _env("YTDLP_IMPERSONATE", default="chrome")

# How many times a download that 403s is re-extracted before the candidate is
# given up on. A 403 on the media fetch usually means the signed URL was
# rejected or went stale between extraction and transfer, and a fresh
# extraction is enough — so this is a genuine retry, not a hopeful one.
YTDLP_403_RETRIES = _int("YTDLP_403_RETRIES", 2)

# Seconds to hold the download queue after this many 403s in a row. Past a
# handful of consecutive refusals it is the exit IP being throttled, not the
# tracks, and continuing simply converts the whole queue into failures at the
# speed the workers can dequeue it.
YTDLP_403_STREAK = _int("YTDLP_403_STREAK", 3)
YTDLP_403_COOLDOWN = _int("YTDLP_403_COOLDOWN", 300)

# Escape hatches for networks, rate limits and bot checks — not everyday
# settings. PO tokens look like "<client>.<context>+<token>", comma separated.
YTDLP_PO_TOKEN = _env("YTDLP_PO_TOKEN")
YTDLP_COOKIES_FILE = _env("YTDLP_COOKIES_FILE")
YTDLP_COOKIES_FROM_BROWSER = _env("YTDLP_COOKIES_FROM_BROWSER")
YTDLP_PROXY = _env("YTDLP_PROXY")
YTDLP_RATE_LIMIT = _env("YTDLP_RATE_LIMIT")
# Some container hosts advertise IPv6 but cannot route to googlevideo.com,
# which shows up as EAI_AGAIN on the AAAA lookup. This binds yt-dlp to IPv4.
YTDLP_FORCE_IPV4 = _env("YTDLP_FORCE_IPV4").lower() in {"1", "true", "yes"}
DOWNLOAD_CONCURRENCY = _int("DOWNLOAD_CONCURRENCY", 2)


def player_clients() -> list[str]:
    return [c.strip() for c in YTDLP_PLAYER_CLIENTS.split(",") if c.strip()]


def po_tokens() -> list[str]:
    return [t.strip() for t in YTDLP_PO_TOKEN.split(",") if t.strip()]


def js_runtimes() -> dict[str, dict]:
    """yt-dlp's ``js_runtimes`` option: ``{"node": {}}``, or empty to disable."""
    return {name.strip().lower(): {} for name in YTDLP_JS_RUNTIMES.split(",") if name.strip()}


def remote_components() -> list[str]:
    return [c.strip() for c in YTDLP_REMOTE_COMPONENTS.split(",") if c.strip()]

TESTING = _env("MUSICDROME_TESTING").lower() in {"1", "true", "yes"}


def music_dir_problem() -> str:
    """Why MUSIC_DIR cannot be written to, or ``""`` if it can.

    ``exists()`` is not the question — an unwritable mount exists perfectly
    happily, which is how a container can boot looking healthy and then fail
    every download at the final ``mkdir``, after the audio has already been
    fetched and encoded. So this actually creates and removes a file.

    The probe name has to be unique per call. Download workers run this
    concurrently, and a shared, fixed name meant two of them raced to unlink
    the same file: the loser's ``unlink`` raised ENOENT, which was reported as
    an unwritable library on a mount that was working perfectly. ``mkstemp``
    creates with ``O_EXCL`` under a name nobody else holds, so the probe now
    only ever fails for the reason it is meant to detect.
    """
    if not MUSIC_DIR.exists():
        return f"{MUSIC_DIR} does not exist — check the volume mount"
    if not MUSIC_DIR.is_dir():
        return f"{MUSIC_DIR} is not a directory"

    try:
        handle, probe = tempfile.mkstemp(prefix=".musicdrome-write-test-", dir=MUSIC_DIR)
    except OSError as exc:
        return _write_probe_failure(exc)

    # Creating the file was the test. Failing to clean up after it is untidy,
    # never a reason to refuse a download.
    os.close(handle)
    try:
        os.unlink(probe)
    except OSError as exc:
        log.debug("could not remove the write probe %s: %s", probe, exc)
    return ""


def _write_probe_failure(exc: OSError) -> str:
    """Turn a failed write probe into something worth acting on.

    The uid and gid we run as and the ones owning the directory are named
    because reconciling those two is the fix — but only for the errnos where
    it *is* the fix. A share that has dropped out underneath the container
    reports ENOENT or ESTALE, and sending someone to PUID/PGID for that is an
    afternoon spent chowning a directory that was never the problem.
    """
    try:
        stat = MUSIC_DIR.stat()
        owner = f"owned by {stat.st_uid}:{stat.st_gid}"
    except OSError:
        owner = "owner unknown"

    running_as = f"Running as {os.getuid()}:{os.getgid()}, directory is {owner}."

    if exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
        return (
            f"{MUSIC_DIR} is not writable ({exc.strerror}). {running_as} "
            f"Set PUID/PGID in .env to a user that can write there, or fix the mount."
        )
    return (
        f"{MUSIC_DIR} could not be written to ({exc.strerror}). {running_as} "
        f"That is not a permissions error — check that the mount behind it is "
        f"still connected and that the host path exists."
    )


def ensure_directories() -> None:
    """Create the directories we own, reporting anything we could not."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    for path in (MUSIC_DIR, PLAYLIST_DIR):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Not fatal — the app still serves suggestions and stats — but it
            # must never be silent, which is what it used to be.
            log.warning("could not create %s: %s", path, exc)


def history_sources() -> list[str]:
    """Which scrobble services are configured well enough to poll."""
    sources = []
    if LASTFM_API_KEY and LASTFM_USER:
        sources.append("lastfm")
    if LISTENBRAINZ_USER:
        sources.append("listenbrainz")
    return sources
