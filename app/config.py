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


def _env_present(*names: str) -> str | None:
    """The first value that is *set*, even when it is empty, else ``None``.

    Distinct from :func:`_env` because for one setting empty is a real answer
    rather than an absent one: ``PLAYLIST_FOLDER=`` means the library root, and
    collapsing that into "unset, use the default" would make the root
    unreachable.
    """
    for name in names:
        if name in os.environ:
            return os.environ[name].strip()
    return None


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

# Where the playlist is written, and the setting most likely to decide whether
# your music server ever imports it.
#
# The folder used to be `_playlists`, hardcoded. That is not a name any server
# refuses — Navidrome's skip list is `$RECYCLE.BIN`, `#snapshot`, `@Recycle`,
# `@Recently-Snapshot`, `.git`, `.streams`, `lost+found` and anything starting
# with a single dot, and an underscore is none of those. But a hardcoded folder
# cannot be pointed at whatever a given server has been told to look in, and
# Navidrome's `ND_PLAYLISTSPATH` is exactly such a setting: left unset it means
# every folder, and the moment it is set it means *only* the folders it names.
#
# So this is the knob that makes the two agree. Four forms:
#
#   playlist            → MUSIC_DIR/playlist    (the default)
#   media/playlists     → MUSIC_DIR/media/playlists
#   "" or "."           → MUSIC_DIR itself, the library root — the most
#                         universally importable spot, since it matches almost
#                         any PlaylistsPath and needs no "../" in any entry
#   /srv/playlists      → used exactly as given, outside the library
#
# Prefer `.` over an empty value for the root: `docker compose` substitutes its
# own default for an empty variable when the compose file says `${VAR:-...}`,
# so an empty one does not always survive the trip.
PLAYLIST_FOLDER = _env_present("MUSICDROME_PLAYLIST_FOLDER", "PLAYLIST_FOLDER")
if PLAYLIST_FOLDER is None:
    PLAYLIST_FOLDER = "playlist"


def _playlist_dir(folder: str) -> Path:
    cleaned = (folder or "").strip().strip('"').strip("'")
    if not cleaned or cleaned == ".":
        return MUSIC_DIR
    path = Path(cleaned).expanduser()
    return path if path.is_absolute() else MUSIC_DIR / path


PLAYLIST_DIR = _playlist_dir(PLAYLIST_FOLDER)

# Where it used to go, so an existing install's playlist can be carried across
# instead of stranded. See :func:`app.download.migrate_playlist_folder`.
LEGACY_PLAYLIST_DIR = MUSIC_DIR / "_playlists"

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

# ─── Navidrome ─────────────────────────────────────────────────────────────
#
# The second signal. Scrobbles say what you played; Navidrome's ``starred`` flag
# says what you went back and *hearted*, which is a different and much scarcer
# statement — a track you loved on purpose rather than one that came up on
# shuffle. Its play counts are the other half: plays that happened while nothing
# was scrobbling still land there.
#
# **There is no such thing as a Navidrome API key.** Its Subsonic API accepts a
# username with either a password or an MD5 token, and nothing else; the native
# REST API takes a JWT its own documentation calls unstable and asks you not to
# use. So this is a username and a password, and Musicdrome never sends the
# password itself — see :func:`app.sources.navidrome.auth_params`, which hashes
# it against a fresh random salt on every single request.
#
# The account only ever needs to read. Navidrome has no per-scope tokens, so the
# credential is as privileged as the account behind it: make a second, ordinary
# (non-admin) Navidrome user for this if that matters to you. Musicdrome calls
# three endpoints — ping, getStarred2 and search3 — and none of them writes.
NAVIDROME_URL = _env("NAVIDROME_URL").rstrip("/")
NAVIDROME_USER = _env("NAVIDROME_USER")
NAVIDROME_PASSWORD = _env("NAVIDROME_PASSWORD")

# Hearts come back in one request. Play counts do not: Subsonic has no "list
# every song I have played" call, so they are read by walking the library with
# search3, which is the one expensive thing here — a 20,000-track library is
# forty requests at the page size below.
#
# That walk is therefore cached rather than repeated. Hearts refresh on every
# scan because they cost a single call and are the signal that actually moves;
# the walk only re-runs once its results are older than this. Set it to 0 to
# walk on every scan, or leave the URL set and NAVIDROME_LIBRARY_PAGE at 0 to
# skip the walk entirely and use hearts alone.
NAVIDROME_LIBRARY_MAX_AGE = _int("NAVIDROME_LIBRARY_MAX_AGE", 21600)  # 6 hours
NAVIDROME_LIBRARY_PAGE = _int("NAVIDROME_LIBRARY_PAGE", 500)
# A stop so a misconfigured or enormous library cannot walk forever.
NAVIDROME_MAX_TRACKS = _int("NAVIDROME_MAX_TRACKS", 100000)

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

# Ollama's context window. Left at 0, each request is sized from what it
# actually sends and expects back — a forty-track scan carrying three hundred
# excluded titles needs several times the window a taste summary does. This
# matters more than it sounds: Ollama's own default is commonly 4096 tokens and
# it drops whatever does not fit *silently*, which is how a scan ends up
# parsing a reply that stopped mid-token and reporting it as a JSON error.
#
# Set OLLAMA_NUM_CTX to pin one value regardless of the request. The ceiling on
# the automatic sizing is worth knowing the cost of: the KV cache for an 8B
# model runs to roughly 128 KB per token, so 16384 is about 2 GB of VRAM held
# for the length of the call.
OLLAMA_NUM_CTX = _int("OLLAMA_NUM_CTX", 0)
OLLAMA_MAX_NUM_CTX = _int("OLLAMA_MAX_NUM_CTX", 16384)

AI_MAX_TOKENS = _int("AI_MAX_TOKENS", 8192)
AI_REQUEST_TIMEOUT = _int("AI_REQUEST_TIMEOUT", 300)

# ─── Downloads ─────────────────────────────────────────────────────────────

# Opus by default, because it is what YouTube and YouTube Music actually serve:
# their best audio stream is Opus at around 160 kbps, so asking for Opus means
# ffmpeg remuxes those bytes into an .opus file with ``-c:a copy`` and never
# re-encodes them. The file is the source, exactly.
#
# The bitrate below is therefore not used on the normal path at all — it only
# applies to the occasional track served as AAC and nothing else. 256 is chosen
# there for one reason: it is past the point where anyone has demonstrated a
# difference, so the question does not have to be asked again. It is not doing
# what a bitrate normally does. Re-encoding cannot recover what the AAC encoder
# already discarded, and a 128 kbps AAC source holds less than Opus needs to be
# transparent — so every bit above roughly 192 is spent storing that encoder's
# artefacts more faithfully. That is a fair price for never having to wonder.
#
# "mp3"/"320" is still a supported answer and was the old default. It costs a
# second lossy encode of an already-lossy source, for a file about twice the
# size that is fractionally *worse* than what YouTube sent — bought in exchange
# for playing on absolutely everything. That trade is worth making for a car
# stereo from 2009 and not much else; a library server transcodes on the fly
# for anything that cannot read Opus.
AUDIO_FORMAT = _env("AUDIO_FORMAT", default="opus").lower()
AUDIO_BITRATE = _env("AUDIO_BITRATE", default="256")
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
    return write_problem(MUSIC_DIR)


def data_dir_problem() -> str:
    """Why DATA_DIR cannot be written to, or ``""`` if it can.

    Worth its own check because it is the first thing to break when PUID
    changes, and because it breaks badly: the database, the scratch space for
    in-flight downloads and yt-dlp's cache all live here, so an unwritable
    /config is fatal where an unwritable /music merely stops downloads. Left to
    sqlite it surfaces as "unable to open database file", which names neither
    the directory, the uid, nor the fact that PUID is what moved.

    Creating it is part of the check rather than a precondition of it. This
    runs before :func:`app.db.init`, which is where ``ensure_directories`` would
    otherwise make it — so a missing directory here is the normal state of a
    fresh install, and reporting it as a fault would be a false alarm every
    first boot. Only a directory that cannot be *made* is worth saying anything
    about.
    """
    if not DATA_DIR.exists():
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _write_probe_failure(DATA_DIR, exc)
    return write_problem(DATA_DIR)


def write_problem(path: Path) -> str:
    """Why ``path`` cannot be written to, or ``""`` if it can."""
    if not path.exists():
        return f"{path} does not exist — check the volume mount"
    if not path.is_dir():
        return f"{path} is not a directory"

    try:
        handle, probe = tempfile.mkstemp(prefix=".musicdrome-write-test-", dir=path)
    except OSError as exc:
        return _write_probe_failure(path, exc)

    # Creating the file was the test. Failing to clean up after it is untidy,
    # never a reason to refuse a download.
    os.close(handle)
    try:
        os.unlink(probe)
    except OSError as exc:
        log.debug("could not remove the write probe %s: %s", probe, exc)
    return ""


def _write_probe_failure(path: Path, exc: OSError) -> str:
    """Turn a failed write probe into something worth acting on.

    The uid and gid we run as and the ones owning the directory are named
    because reconciling those two is the fix — but only for the errnos where
    it *is* the fix. A share that has dropped out underneath the container
    reports ENOENT or ESTALE, and sending someone to PUID/PGID for that is an
    afternoon spent chowning a directory that was never the problem.
    """
    try:
        stat = path.stat()
        owner = f"owned by {stat.st_uid}:{stat.st_gid}"
    except OSError:
        owner = "owner unknown"

    running_as = f"Running as {os.getuid()}:{os.getgid()}, directory is {owner}."

    if exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
        return (
            f"{path} is not writable ({exc.strerror}). {running_as} "
            f"Set PUID/PGID in .env to a user that can write there, or fix the mount."
        )
    return (
        f"{path} could not be written to ({exc.strerror}). {running_as} "
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


def navidrome_configured() -> bool:
    """Whether Navidrome has all three of the things it needs.

    All three, because two of them is not a degraded configuration — it is a
    typo. A URL with no password fails every request with "wrong username or
    password", which reads like a rejected credential rather than a missing one.
    """
    return bool(NAVIDROME_URL and NAVIDROME_USER and NAVIDROME_PASSWORD)
