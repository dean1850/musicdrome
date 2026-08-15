"""Finding the audio for a track, and filing it.

Two searches, in order. YouTube Music first, through ``ytmusicapi``: it returns
a real catalogue with artist, album and duration as structured fields, so a
candidate can be checked rather than guessed at. Plain YouTube second, through
yt-dlp, for the tracks YouTube Music does not carry — that path has only a
title string to go on, so it leans on keyword scoring and is trusted less.

Nothing is downloaded on a weak match. A card that cannot be matched
confidently is marked failed with the reason, which is a better outcome than
quietly filing a karaoke version into the library.

**On player clients and SABR.** YouTube serves different clients differently,
and which ones still hand out plain HTTPS format URLs changes every few months.
Clients it has moved onto SABR return a player response with no format URLs at
all, which yt-dlp reports as "Some <client> client https formats have been
skipped as they are missing a URL" (yt-dlp/yt-dlp#12482). The clients that do
still carry URLs need a JavaScript runtime to solve YouTube's signature and
n-challenges, so the image ships Deno and the solver scripts that run on it —
see :data:`app.config.YTDLP_JS_RUNTIMES`.

The client list itself is deliberately *not* pinned. yt-dlp's own defaults move
with YouTube; a list written down here does not, and a stale pin is worse than
no pin because it puts known-broken clients first.

**On HTTP 403.** The search succeeds, the metadata call succeeds, and then the
media fetch is refused. That is YouTube declining the *connection*, not the
video: the exit address looks like a datacenter or a VPN, or the signed media
URL went stale between extraction and transfer. Four things answer it, in the
order they get a chance to:

1. the TLS handshake impersonates Chrome (:func:`impersonate_target`), which is
   what stops the connection looking like a script in the first place;
2. a 403 mid-download is retried against freshly extracted URLs;
3. failing that, the next-best candidate is tried, since a different upload of
   the same track is often served without complaint;
4. and once several downloads in a row have 403'd, the queue pauses instead of
   converting every remaining track into a failure at dequeue speed.

Downloads run on a small pool of worker threads draining the ``downloads``
table, so the queue survives a restart: anything left mid-flight is requeued at
boot by :func:`start_workers`.
"""

from __future__ import annotations

import base64
import logging
import os
import queue
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from . import config, db
from .norm import artist_key, safe_filename, title_key, track_key

log = logging.getLogger(__name__)

# Titles that usually mean "not the studio recording that was asked for".
NEGATIVE = re.compile(
    r"\b(live|cover|karaoke|instrumental|remix|reaction|lyrics?\s*video|"
    r"sped\s*up|slowed|nightcore|8d\s*audio|tutorial|reverb|mashup|"
    r"full\s*album|mix\s*\d{4}|extended\s*mix)\b",
    re.IGNORECASE,
)

MIN_SCORE = 0.50   # below this, no download is attempted at all
GOOD_SCORE = 0.75  # above this on YouTube Music, plain YouTube is not consulted

# yt-dlp warnings that are expected and harmless: they mean one format source
# was skipped while other clients still served usable audio. Left unsuppressed
# they bury the warnings that do matter.
_QUIET_WARNINGS = (
    "GVS PO Token which was not provided",
    "Some tv client https formats have been skipped as they are DRM",
    "Signature solving failed: Some formats may be missing",
    "n challenge solving failed: Some formats may be missing",
    "Some web client https formats have been skipped",
    # SABR. yt-dlp emits this once per client per video whenever it tries a
    # client YouTube has moved off plain HTTPS formats, then falls through to
    # one that works. It is a running commentary on yt-dlp's fallback order,
    # not a failure of ours, and at one line per candidate it drowns out
    # everything else. A download that genuinely finds no usable format fails
    # with its own error, which is not filtered.
    "formats have been skipped as they are missing a URL",
)

# FFmpegExtractAudio names its output after the codec, except Vorbis.
_EXTENSIONS = {"vorbis": "ogg"}

_queue: "queue.Queue[int]" = queue.Queue()
_workers: list[threading.Thread] = []
_progress: dict[int, int] = {}
_ytmusic_client: Any = None
_ytmusic_lock = threading.Lock()

# Appends to the one playlist are serialised: two workers finishing at the same
# moment would otherwise interleave an #EXTINF with somebody else's path.
_playlist_lock = threading.Lock()

# How many downloads have 403'd in a row, and when the queue may resume. Shared
# across the worker pool, hence the lock.
_403_lock = threading.Lock()
_403_streak = 0
_403_until = 0.0

_UNSET = object()
_impersonate_cache: Any = _UNSET
_impersonate_reason = ""

# yt-dlp reports a refusal in a few shapes ("HTTP Error 403: Forbidden",
# "fragment 1 not found ... 403"), all of which carry the status code. Matching
# the number rather than the word matters: yt-dlp's errors quote video titles,
# and a track called "Forbidden" must not be mistaken for a refusal and sent
# round the retry loop.
_FORBIDDEN = re.compile(r"\b403\b")

# yt-dlp's refusal when nothing can serve the requested impersonation target:
# 'Impersonate target "chrome" is not available. Use --list-impersonate-targets
# to see available targets.' It is raised per request, before anything is
# fetched, so it fails searches and downloads alike.
_NO_IMPERSONATION = re.compile(r"impersonate target.{0,80}?is not available", re.IGNORECASE | re.DOTALL)


class DownloadError(RuntimeError):
    pass


def is_forbidden(error: BaseException | str) -> bool:
    """Whether an error is YouTube refusing the connection rather than us."""
    return bool(_FORBIDDEN.search(str(error)))


def is_impersonation_unavailable(error: BaseException | str) -> bool:
    """Whether yt-dlp refused the request because it cannot impersonate at all."""
    return bool(_NO_IMPERSONATION.search(str(error)))


@dataclass
class Candidate:
    url: str
    title: str
    artist: str = ""
    album: str = ""
    duration: int = 0
    source: str = "youtube"
    score: float = field(default=0.0)


@dataclass
class Fetched:
    """A finished download, and what it cost in quality to get there.

    Recorded because "no re-encode" is a claim worth being able to check. The
    normal path copies YouTube's Opus stream through untouched, but a track
    served only as AAC is converted, and nothing in the resulting file says
    which of those happened — the two are indistinguishable on disk.
    """

    path: Path
    source_codec: str = ""
    source_abr: int = 0
    encoded: str = ""  # "copied", "converted", or "" when the source is unknown


# What yt-dlp calls a codec, mapped onto what we call it. YouTube reports AAC
# as its MPEG-4 object type ("mp4a.40.2"), which is the same codec by a name
# that cannot be compared with anything.
_CODEC_NAMES = {"mp4a": "aac", "m4a": "aac", "aac": "aac", "opus": "opus",
                "vorbis": "vorbis", "mp3": "mp3", "mp4a.40.2": "aac", "flac": "flac"}

# And the codec each container we produce holds, for deciding whether the file
# on disk still contains the bytes that were downloaded.
_EXTENSION_CODECS = {"opus": "opus", "m4a": "aac", "mp3": "mp3", "flac": "flac",
                     "ogg": "vorbis", "wav": "pcm"}


def codec_name(raw: Any) -> str:
    """A codec name that can be compared. ``mp4a.40.2`` and ``aac`` are one thing."""
    base = str(raw or "").split(".")[0].strip().lower()
    if not base or base == "none":
        return ""
    return _CODEC_NAMES.get(base, base)


def encoding_of(source_codec: str, extension: str) -> str:
    """Whether a file with this extension still holds the bytes that were served.

    Decided by comparing the served codec with what the container we produced
    can hold, rather than by predicting what ffmpeg would do — that would be a
    second copy of yt-dlp's decision rules, free to drift out of step with the
    first. ``""`` when the source is unknown, because a finished file cannot
    say what it used to be and a guess here would be indistinguishable from a
    measurement.
    """
    if not source_codec:
        return ""
    return "copied" if source_codec == _EXTENSION_CODECS.get(extension) else "converted"


def _source_audio(info: Any) -> tuple[str, int]:
    """The codec and bitrate of the stream YouTube actually served.

    Read from the format yt-dlp selected rather than from the finished file,
    because the finished file cannot say what it used to be.
    """
    if not isinstance(info, dict):
        return "", 0

    codec, abr = "", 0.0
    for entry in (info, *(info.get("requested_downloads") or [])):
        if not isinstance(entry, dict):
            continue
        codec = codec or codec_name(entry.get("acodec"))
        try:
            abr = abr or float(entry.get("abr") or 0)
        except (TypeError, ValueError):
            pass
    return codec, int(round(abr))


def audio_extension() -> str:
    fmt = config.AUDIO_FORMAT or "opus"
    return _EXTENSIONS.get(fmt, fmt)


def format_sort() -> list[str]:
    """Ask YouTube for the stream that needs no re-encode, when there is one.

    yt-dlp's ``bestaudio`` sorts by bitrate, which usually lands on Opus but is
    free not to. That matters because ffmpeg only copies the audio through
    untouched when the source codec already *is* the configured format — the
    moment it picks the AAC stream instead, an Opus library costs a second
    lossy encode of an already-lossy source for no benefit whatsoever.

    Naming the codec removes the "usually". Nothing is pinned beyond that: this
    is a preference, so a track served only as AAC still downloads and is
    converted. For formats YouTube does not serve at all — mp3, flac — there is
    nothing to prefer and yt-dlp's own ordering is left alone.
    """
    codec = {"opus": "opus", "m4a": "aac", "aac": "aac", "vorbis": "vorbis"}.get(
        config.AUDIO_FORMAT
    )
    return [f"acodec:{codec}"] if codec else []


class _YtdlpLogger:
    """Routes yt-dlp's own output into our logger, minus the known noise."""

    @staticmethod
    def debug(message: str) -> None:
        pass

    @staticmethod
    def info(message: str) -> None:
        pass

    @staticmethod
    def warning(message: str) -> None:
        if not any(fragment in message for fragment in _QUIET_WARNINGS):
            log.warning("yt-dlp: %s", message)

    @staticmethod
    def error(message: str) -> None:
        log.debug("yt-dlp: %s", message)  # the caller reports the failure itself


def js_runtime_problem() -> str:
    """Why YouTube downloads will be degraded, or ``""`` if a runtime is usable.

    Worth a check of its own because the failure is silent. yt-dlp does not
    refuse to start when the configured runtime is missing or too old — it
    drops to its JS-less clients and carries on, and the first sign anything is
    wrong is HTTP 403 partway through a download, or a match that finds no
    formats. Debian's Node is below yt-dlp's minimum, which is exactly how this
    image shipped a runtime that was never once used.

    Reports the state, never enforces it: a runtime yt-dlp will not touch is a
    reason to say so loudly at boot, not a reason to refuse to run.
    """
    wanted = config.js_runtimes()
    if not wanted:
        return ""  # explicitly disabled — the operator already knows

    # Snapshot the names first: yt-dlp drops the ones it does not recognise by
    # popping them out of the very dict it is handed, so reading them back
    # afterwards reports nothing at all.
    names = ", ".join(sorted(wanted))

    import yt_dlp

    try:
        with yt_dlp.YoutubeDL(
            {"quiet": True, "logger": _YtdlpLogger(), "js_runtimes": dict(wanted)}
        ) as ydl:
            detected = ydl._js_runtimes
    except Exception as exc:
        # Private attribute, so it is allowed to disappear. Losing the warning
        # is a fair price; failing to boot over it is not.
        log.debug("could not inspect yt-dlp's JavaScript runtimes: %s", exc)
        return ""

    if not detected:
        # yt-dlp drops names it does not know during construction, so an empty
        # result means every name configured was a typo.
        return (
            f"yt-dlp recognises none of the runtimes in YTDLP_JS_RUNTIMES "
            f"({names}) — YouTube downloads will fail with 403."
        )

    usable, problems = [], []
    for name, runtime in sorted(detected.items()):
        info = getattr(runtime, "info", None) if runtime is not None else None
        if info is None:
            problems.append(f"{name} was not found")
        elif info.supported is False:
            problems.append(f"{name} {info.version} is older than yt-dlp accepts")
        else:
            usable.append(f"{name} {info.version}")

    if usable:
        log.info("javascript: %s", ", ".join(usable))
        return ""
    return (
        f"no usable JavaScript runtime ({'; '.join(problems)}) — YouTube downloads "
        f"will fail with 403 or find no formats. Install one yt-dlp accepts "
        f"(deno >= 2.3, node >= 22) or point YTDLP_JS_RUNTIMES at it."
    )


# ─── Browser impersonation ─────────────────────────────────────────────────


def impersonate_target() -> Any | None:
    """The browser yt-dlp should impersonate, or ``None`` to send our own TLS.

    YouTube fingerprints the handshake — cipher order, extensions, ALPN, HTTP/2
    settings — and python's stack has a signature nothing else on the internet
    shares. From a home address that is usually let through; from a VPN or a
    datacenter it is a large part of why the media fetch comes back 403 when
    the search that preceded it was answered normally.

    Every failure path here returns ``None`` rather than raising. Asking for a
    target yt-dlp cannot provide makes *every* request fail with
    "Impersonate target is not available", so an absent curl_cffi has to mean
    "carry on without it", not "download nothing".
    """
    global _impersonate_cache, _impersonate_reason
    if _impersonate_cache is not _UNSET:
        return _impersonate_cache

    _impersonate_cache = None
    name = config.YTDLP_IMPERSONATE.strip()
    if not name:
        return None

    # Imported for its side effect of proving it works, not for anything it
    # exports: a wheel that unpacked but cannot find its bundled libcurl raises
    # here, and that is exactly as unusable as not being installed at all.
    try:
        import curl_cffi  # noqa: F401 — the only backend yt-dlp impersonates with
    except Exception as exc:
        _impersonate_reason = f"curl_cffi is not usable ({exc})"
        log.debug("curl_cffi is unusable (%s); TLS impersonation is off", exc)
        return None

    try:
        import yt_dlp
        from yt_dlp.networking.impersonate import ImpersonateTarget

        target = ImpersonateTarget.from_str(name)
    except Exception as exc:
        _impersonate_reason = f"YTDLP_IMPERSONATE={name} is not a target yt-dlp knows ({exc})"
        log.debug("YTDLP_IMPERSONATE=%s is not a target yt-dlp knows: %s", name, exc)
        return None

    # Ask yt-dlp what its request handlers can actually serve. Private API, so
    # an inspection that raises is treated as "probably fine" and left to the
    # runtime fallback in :func:`_extract_info` to disprove.
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "logger": _YtdlpLogger()}) as ydl:
            available = ydl._get_available_impersonate_targets()
    except Exception as exc:
        log.debug("could not enumerate impersonation targets: %s", exc)
        _impersonate_cache = target
        return target

    # An empty list is the whole answer, not a missing one: it means no request
    # handler registered any target at all. This used to read `if available and
    # not any(...)`, which skipped the check precisely when it mattered — a
    # curl_cffi yt-dlp declines to load imports perfectly well, enumerates to
    # nothing, and was cached here as a valid target. The boot log then said
    # "impersonating chrome" while every subsequent request died with
    # 'Impersonate target "chrome" is not available'.
    if not available:
        _impersonate_reason = _no_handler_reason(curl_cffi)
        log.debug("no impersonation targets are available: %s", _impersonate_reason)
        return None

    if not any(candidate in target for candidate, _ in available):
        offered = ", ".join(str(candidate) for candidate, _ in available[:4])
        _impersonate_reason = f"no request handler offers '{name}' (available: {offered})"
        log.debug("no request handler can impersonate '%s'; continuing without it", name)
        return None

    _impersonate_cache = target
    return target


def _no_handler_reason(curl_cffi: Any) -> str:
    """Why curl_cffi imported cleanly and still produced no targets.

    Almost always a version gate. yt-dlp pins the curl_cffi range it was built
    against and enforces it at import time, so a curl_cffi outside that window
    raises out of ``yt_dlp.networking._curlcffi`` and the handler is never
    registered — no warning, no targets, and a clear ImportError that nobody
    ever sees because it is caught during handler discovery. Surfacing that
    message verbatim turns an unexplained failure into an actionable one, since
    it names the versions that would work.
    """
    version = getattr(curl_cffi, "__version__", "unknown")
    try:
        import yt_dlp.networking._curlcffi  # noqa: F401
    except ImportError as exc:
        return f"curl_cffi {version} is installed but yt-dlp will not load it — {exc}"
    except Exception as exc:
        return f"curl_cffi {version} is installed but its yt-dlp handler failed: {exc}"
    return f"curl_cffi {version} is installed but no request handler registered a target"


def _drop_impersonation(exc: BaseException) -> None:
    """Stop asking for a target yt-dlp has just told us it does not have.

    The boot check can be right at boot and wrong an hour later: the entrypoint
    upgrades yt-dlp on every start, and a yt-dlp that moves its supported
    curl_cffi window lands on an install whose curl_cffi has not moved with it.
    Taking the hint from the first refusal costs one failed request; not taking
    it costs every download until somebody reads the logs.
    """
    global _impersonate_cache, _impersonate_reason
    if _impersonate_cache is not None:
        _impersonate_cache = None
        _impersonate_reason = f"yt-dlp refused the target at runtime ({exc})"
        log.debug("impersonation off after yt-dlp refused the target: %s", exc)


def provenance(source_codec: str, source_abr: int, encoded: str) -> str:
    """One phrase for what a download was, and what happened to it.

    "opus 160k copied" is the answer to the only question worth asking about a
    file YouTube served: is this still the audio they sent, or a second encode
    of it? Reused by the log line and the downloads table so both say the same
    thing.
    """
    if not source_codec:
        return "source unknown"
    rate = f" {source_abr}k" if source_abr else ""
    return f"{source_codec}{rate} {encoded}".strip()


def audio_status() -> str:
    """One line for the boot log: what lands in the library, and at what cost.

    Worth saying out loud because the answer changed. Installs that never set
    AUDIO_FORMAT used to get MP3 320 and now get Opus, and the boot log is the
    one place that difference is visible before the first download.
    """
    fmt = config.AUDIO_FORMAT or "opus"
    if format_sort():
        return (
            f"{fmt} — copied from YouTube's own {fmt} stream where there is one "
            f"(no re-encode), otherwise converted at {config.AUDIO_BITRATE} kbps"
        )
    if fmt in {"flac", "wav", "alac"}:
        return (
            f"{fmt} — lossless container around a lossy source, so it is larger "
            f"than the download without being better than it"
        )
    return f"{fmt} at {config.AUDIO_BITRATE} kbps — re-encoded from YouTube's audio"


def impersonation_status() -> str:
    """One line for the boot log: what the connection will look like."""
    target = impersonate_target()
    if target:
        return f"impersonating {target}"
    if not config.YTDLP_IMPERSONATE.strip():
        return "off (YTDLP_IMPERSONATE is empty)"
    return (
        f"off — {_impersonate_reason or 'curl_cffi is unavailable'}. YouTube sees a "
        "python TLS fingerprint; expect HTTP 403 on the media fetch from VPN or "
        "datacenter addresses."
    )


def _extract_info(yt_dlp: Any, options: dict[str, Any], url: str, *, download: bool) -> Any:
    """``extract_info``, retried once without impersonation if that is what failed.

    Impersonation is an optimisation — it is what keeps YouTube from answering
    403 on a datacenter exit — but an unavailable target is fatal to the
    request rather than degrading it, and it fails before a single byte is
    fetched. Retrying without it converts "nothing downloads" into "downloads
    work, with a python TLS fingerprint", which is strictly the better failure.
    """
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=download)
    except Exception as exc:
        if "impersonate" not in options or not is_impersonation_unavailable(exc):
            raise
        _drop_impersonation(exc)
        # The caller's dict is mutated deliberately: the 403 retry loop in
        # :func:`_extract_with_retry` reuses these options, and would otherwise
        # put the dead target straight back on the wire on its next attempt.
        options.pop("impersonate", None)
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=download)


# ─── Candidate scoring ─────────────────────────────────────────────────────


def score(candidate: Candidate, artist: str, title: str, expected: int = 0) -> float:
    """How much this candidate looks like the track we asked for, 0-1.

    Duration is the strongest signal available: two recordings that agree on
    artist, title and length to within a few seconds are almost always the same
    master, and a "cover" that agrees on all three is one worth having anyway.
    """
    want_artist, want_title = artist_key(artist), title_key(title)
    haystack = f"{candidate.artist} {candidate.title} {candidate.album}".casefold()
    value = 0.0
    attributed = True

    if candidate.artist and artist_key(candidate.artist) == want_artist:
        value += 0.45
    elif want_artist and want_artist in artist_key(haystack):
        value += 0.30
    else:
        attributed = False

    if title_key(candidate.title) == want_title:
        value += 0.35
    elif want_title and want_title in title_key(candidate.title):
        value += 0.20

    # Only penalise "live"/"remix" when the track we want is not itself one.
    if NEGATIVE.search(candidate.title) and not NEGATIVE.search(f"{artist} {title}"):
        value -= 0.40

    if expected and candidate.duration:
        drift = abs(candidate.duration - expected)
        value += 0.25 if drift <= 3 else 0.15 if drift <= 10 else 0.0 if drift <= 25 else -0.35
    elif candidate.duration and not (45 <= candidate.duration <= 900):
        value -= 0.30  # without a reference, reject things that are not one track

    if candidate.source == "ytmusic":
        value += 0.05

    # A candidate we cannot attribute to the right artist is never downloaded,
    # however well the title and length line up. That combination is what a
    # tribute band, a re-upload or a soundalike looks like, and filing one of
    # those into the library is worse than coming back empty-handed.
    if not attributed:
        value = min(value, MIN_SCORE - 0.05)

    return max(0.0, min(1.0, value))


# ─── Search ────────────────────────────────────────────────────────────────


def _ytmusic():
    global _ytmusic_client
    with _ytmusic_lock:
        if _ytmusic_client is None:
            from ytmusicapi import YTMusic

            _ytmusic_client = YTMusic()
        return _ytmusic_client


def search_ytmusic(artist: str, title: str, limit: int = 8) -> list[Candidate]:
    """Search the YouTube Music catalogue."""
    try:
        results = _ytmusic().search(f"{artist} {title}", filter="songs", limit=limit)
    except Exception as exc:
        log.warning("YouTube Music search failed for %s - %s: %s", artist, title, exc)
        return []

    candidates = []
    for entry in results or []:
        video_id = entry.get("videoId")
        if not video_id:
            continue
        candidates.append(
            Candidate(
                url=f"https://music.youtube.com/watch?v={video_id}",
                title=entry.get("title", ""),
                artist=", ".join(a.get("name", "") for a in entry.get("artists") or []),
                album=(entry.get("album") or {}).get("name", ""),
                duration=int(entry.get("duration_seconds") or 0),
                source="ytmusic",
            )
        )
    return candidates


def _ydl_options(**overrides) -> dict[str, Any]:
    options: dict[str, Any] = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": False,  # warnings go through _YtdlpLogger, which filters
        "noprogress": True,
        "logger": _YtdlpLogger(),
        "noplaylist": True,
        "nocheckcertificate": True,
        "overwrites": True,
        # googlevideo CDN hosts are short-lived shards; a single transient
        # timeout used to abort a whole download.
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "extractor_args": {"youtube": {}},
        # A JS runtime plus yt-dlp's external JS components, which is what the
        # clients that still serve real format URLs require — see the module
        # docstring. Without these, every client is either SABR-only or
        # unsolvable and nothing downloads.
        "js_runtimes": config.js_runtimes(),
        "remote_components": config.remote_components(),
        # Light pacing so back-to-back downloads do not trigger a 429.
        "sleep_interval_requests": 1,
    }

    # Prefer the source stream that is already in the configured format, so
    # ffmpeg remuxes rather than re-encodes.
    sort = format_sort()
    if sort:
        options["format_sort"] = sort

    # A Chrome TLS fingerprint instead of python's, when curl_cffi can provide
    # one. Left out entirely otherwise: an unavailable target fails every
    # request, which would be a far worse problem than the one it solves.
    target = impersonate_target()
    if target is not None:
        options["impersonate"] = target

    # Only ever sent when explicitly configured. Left unset, yt-dlp chooses,
    # and its choice tracks YouTube's current behaviour.
    clients = config.player_clients()
    if clients:
        options["extractor_args"]["youtube"]["player_client"] = clients

    tokens = config.po_tokens()
    if tokens:
        options["extractor_args"]["youtube"]["po_token"] = tokens
    if config.YTDLP_FORCE_IPV4:
        options["source_address"] = "0.0.0.0"
    if config.YTDLP_PROXY:
        options["proxy"] = config.YTDLP_PROXY
    if config.YTDLP_COOKIES_FILE:
        options["cookiefile"] = config.YTDLP_COOKIES_FILE
    if config.YTDLP_COOKIES_FROM_BROWSER:
        parts = config.YTDLP_COOKIES_FROM_BROWSER.split(":", 1)
        options["cookiesfrombrowser"] = tuple(parts)
    if config.YTDLP_RATE_LIMIT:
        try:
            options["ratelimit"] = int(config.YTDLP_RATE_LIMIT)
        except ValueError:
            pass

    options.update(overrides)
    return options


def search_youtube(artist: str, title: str, limit: int = 5) -> list[Candidate]:
    """Fall back to a plain YouTube search.

    Flat extraction matters here. Resolving each hit in full means a complete
    player round-trip per result — five of them, to pick one — which is both
    the bulk of a search's wall time and the reason a single failed match used
    to emit a screenful of client warnings. The search page already carries the
    title, duration and channel that scoring needs; formats are only resolved
    for the one candidate that actually wins.
    """
    import yt_dlp

    try:
        info = _extract_info(
            yt_dlp,
            _ydl_options(skip_download=True, extract_flat="in_playlist"),
            f"ytsearch{limit}:{artist} {title} audio",
            download=False,
        )
    except Exception as exc:
        log.warning("YouTube search failed for %s - %s: %s", artist, title, exc)
        return []

    entries = (info or {}).get("entries") or []
    candidates = []
    for entry in entries:
        url = (entry or {}).get("webpage_url") or (entry or {}).get("url")
        if not url:
            continue
        candidates.append(
            Candidate(
                url=url,
                title=entry.get("title", ""),
                artist=_channel_artist(entry),
                album=entry.get("album") or "",
                duration=int(entry.get("duration") or 0),
                source="youtube",
            )
        )
    return candidates


def _channel_artist(entry: dict[str, Any]) -> str:
    """The artist to credit a plain-YouTube hit to.

    YouTube auto-generates a channel per artist for label-delivered music and
    names it "<Artist> - Topic". Those are the closest thing a plain YouTube
    search has to catalogue metadata, so the suffix is stripped and what is
    left is treated as the artist. Scoring refuses to download anything it
    cannot attribute, so recovering this is the difference between a Topic
    upload matching and being thrown away as unattributable.
    """
    name = (entry.get("artist") or entry.get("channel") or entry.get("uploader") or "").strip()
    return re.sub(r"\s*-\s*Topic$", "", name, flags=re.IGNORECASE).strip()


def ranked_matches(artist: str, title: str, expected: int = 0, limit: int = 3) -> list[Candidate]:
    """Every credible candidate across both sources, best first.

    More than one is worth having because YouTube's refusals are per-upload: a
    track that answers 403 from one video id is regularly served without
    complaint from another upload of the same recording, so a download that
    hits a wall has somewhere to go before it is called a failure.
    """
    ranked: list[Candidate] = []

    for candidate in search_ytmusic(artist, title):
        candidate.score = score(candidate, artist, title, expected)
        ranked.append(candidate)

    ranked.sort(key=lambda c: c.score, reverse=True)
    # A strong YouTube Music hit is trusted on its own; plain YouTube is only
    # consulted when nothing in the catalogue was convincing enough.
    if not ranked or ranked[0].score < GOOD_SCORE:
        for candidate in search_youtube(artist, title):
            candidate.score = score(candidate, artist, title, expected)
            ranked.append(candidate)
        ranked.sort(key=lambda c: c.score, reverse=True)

    return [candidate for candidate in ranked if candidate.score >= MIN_SCORE][:limit]


def best_match(artist: str, title: str, expected: int = 0) -> Candidate | None:
    """The best candidate across both sources, or ``None`` if none is credible."""
    ranked = ranked_matches(artist, title, expected, limit=1)
    return ranked[0] if ranked else None


# ─── Tagging and filing ────────────────────────────────────────────────────


def _fetch_cover(url: str) -> bytes:
    if not url:
        return b""
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            response = client.get(url)
        if response.status_code == 200 and len(response.content) < 8_000_000:
            return response.content
    except httpx.HTTPError:
        pass
    return b""


def _cover_mime(cover: bytes) -> str:
    return "image/png" if cover[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"


def _ogg_picture(cover: bytes) -> str:
    """Cover art as Ogg carries it: a FLAC picture block, base64, in a comment.

    Ogg has no picture field of its own. The convention every player follows —
    Navidrome and taglib included — is a ``METADATA_BLOCK_PICTURE`` comment
    holding a base64-encoded FLAC picture block, which is what this builds.
    Without it an Opus library is a library with no artwork in it.
    """
    from mutagen.flac import Picture

    picture = Picture()
    picture.data = cover
    picture.type = 3  # front cover
    picture.mime = _cover_mime(cover)
    picture.desc = "Cover"
    return base64.b64encode(picture.write()).decode("ascii")


def tag(path: Path, meta: dict[str, Any]) -> None:
    """Write tags and embed cover art, in whichever container we produced."""
    import mutagen

    fields = {
        "artist": meta.get("artist", ""),
        "title": meta.get("title", ""),
        "album": meta.get("album", ""),
        "albumartist": meta.get("artist", ""),
        "date": meta.get("year", ""),
        "genre": (meta.get("tags") or "").split(",")[0],
    }
    if meta.get("track_no"):
        fields["tracknumber"] = str(meta["track_no"])
    if meta.get("recording_mbid"):
        fields["musicbrainz_trackid"] = meta["recording_mbid"]

    suffix = path.suffix.lower()
    cover = _fetch_cover(meta.get("cover_url", ""))

    try:
        if suffix == ".mp3":
            from mutagen.easyid3 import EasyID3
            from mutagen.id3 import APIC, ID3, ID3NoHeaderError
            from mutagen.mp3 import MP3

            try:
                audio = EasyID3(path)
            except ID3NoHeaderError:
                mp3 = MP3(path)
                mp3.add_tags()
                mp3.save()
                audio = EasyID3(path)
            for key, value in fields.items():
                if value:
                    audio[key] = str(value)
            audio.save()

            if cover:
                id3 = ID3(path)
                id3.delall("APIC")
                id3.add(APIC(encoding=3, mime=_cover_mime(cover), type=3,
                             desc="Cover", data=cover))
                id3.save(path)
            return

        audio = mutagen.File(path, easy=True)
        if audio is None:
            return
        if audio.tags is None:
            audio.add_tags()
        for key, value in fields.items():
            if value:
                try:
                    audio[key] = str(value)
                except (KeyError, ValueError):
                    pass  # this container has no such field
        audio.save()

        if not cover:
            return

        if suffix == ".flac":
            from mutagen.flac import FLAC, Picture

            picture = Picture()
            picture.data, picture.type, picture.mime = cover, 3, _cover_mime(cover)
            flac = FLAC(path)
            flac.clear_pictures()
            flac.add_picture(picture)
            flac.save()
        elif suffix in {".m4a", ".mp4"}:
            from mutagen.mp4 import MP4, MP4Cover

            fmt = MP4Cover.FORMAT_PNG if _cover_mime(cover) == "image/png" else MP4Cover.FORMAT_JPEG
            mp4 = MP4(path)
            mp4["covr"] = [MP4Cover(cover, imageformat=fmt)]
            mp4.save()
        elif suffix in {".opus", ".ogg", ".oga"}:
            # The same object the fields were just written through: Ogg tags
            # are free-form Vorbis comments, so the picture is one more of them.
            audio["metadata_block_picture"] = [_ogg_picture(cover)]
            audio.save()
        else:
            log.debug("no cover-art support for %s files", suffix)
    except Exception as exc:
        log.warning("could not fully tag %s: %s", path, exc)


def target_path(artist: str, album: str, title: str, track_no: int = 0,
                extension: str | None = None) -> Path:
    """``MUSIC_DIR/Artist/Album/01 - Title.mp3``, never overwriting."""
    suffix = extension or audio_extension()
    artist_part = safe_filename(artist, "Unknown Artist")
    album_part = safe_filename(album, "Singles")
    title_part = safe_filename(title, "Untitled")
    prefix = f"{track_no:02d} - " if track_no else ""

    directory = config.MUSIC_DIR / artist_part / album_part
    directory.mkdir(parents=True, exist_ok=True)

    target = directory / f"{prefix}{title_part}.{suffix}"
    if not target.exists():
        return target
    for index in range(2, 100):
        alternative = directory / f"{prefix}{title_part} ({index}).{suffix}"
        if not alternative.exists():
            return alternative
    raise DownloadError(f"too many files named {title_part} in {directory}")


def playlist_entry(path: Path, playlist_dir: Path | None = None) -> str:
    """Where a downloaded file sits, written relative to the playlist.

    Relative so the library can be moved, or mounted at a different path in
    whatever plays it, without every line breaking. Navidrome resolves a
    relative entry against the folder the playlist itself is in, which is what
    makes this the portable form rather than merely the shorter one.

    Computed from the playlist's actual location rather than assumed. This used
    to prepend a literal ``".."``, which was right for exactly one layout — the
    hardcoded ``_playlists`` one level under the library — and silently wrong
    for every other. At the library root it would climb out of the library
    altogether; two levels down it would not climb far enough. Both produce a
    playlist full of paths that resolve to nothing, which a music server
    imports as an empty playlist rather than reporting as broken.

    Absolute when the file and the playlist do not share the library, because
    a relative path between two unrelated trees is a long chain of ``..`` that
    breaks the moment either end moves.
    """
    playlist_dir = config.PLAYLIST_DIR if playlist_dir is None else playlist_dir
    try:
        path.relative_to(config.MUSIC_DIR)
        playlist_dir.relative_to(config.MUSIC_DIR)
    except ValueError:
        return str(path)

    try:
        return Path(os.path.relpath(path, playlist_dir)).as_posix()
    except ValueError:
        # Different drives on Windows: no relative path exists between them.
        return str(path)


def playlist_paths(playlist: Path) -> set[str]:
    """The file paths already listed in a playlist, ignoring the #EXTINF lines."""
    try:
        text = playlist.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    return {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.startswith("#")
    }


def append_to_playlist(path: Path, meta: dict[str, Any]) -> str:
    """Add a finished download to the one Musicdrome playlist.

    One file for the life of the install, not one per scan. Per-scan playlists
    meant a library server accumulated a musicdrome-scan-0001, -0002, -0003
    that nobody opened twice, and no single place to hear what Musicdrome has
    actually brought in.

    Appending is idempotent: a track already listed is not listed again, so a
    re-download or a retry cannot double an entry.
    """
    entry = playlist_entry(path)
    playlist = config.PLAYLIST_PATH

    with _playlist_lock:
        try:
            config.PLAYLIST_DIR.mkdir(parents=True, exist_ok=True)
            if entry in playlist_paths(playlist):
                return str(playlist)

            new_file = not playlist.exists()
            with playlist.open("a", encoding="utf-8") as handle:
                if new_file:
                    handle.write("#EXTM3U\n")
                handle.write(
                    f"#EXTINF:{meta.get('duration', 0)},"
                    f"{meta.get('artist', '')} - {meta.get('title', '')}\n"
                    f"{entry}\n"
                )
        except OSError as exc:
            # The file is on disk and the row is written; a playlist that could
            # not be updated is worth a line in the log and nothing more.
            log.warning("could not update %s: %s", playlist, exc)
            return ""
    return str(playlist)


def _parse_playlist(playlist: Path) -> list[tuple[str, str]]:
    """``(#EXTINF line, path)`` pairs from an .m3u, in order."""
    try:
        lines = playlist.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        log.warning("could not read %s: %s", playlist, exc)
        return []

    entries: list[tuple[str, str]] = []
    extinf = ""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            # Anything else (#EXTM3U, comments) is not carried across.
            extinf = stripped if stripped.startswith("#EXTINF:") else extinf
            continue
        entries.append((extinf, stripped))
        extinf = ""
    return entries


def rewrite_entry(entry: str, old_dir: Path, new_dir: Path) -> str:
    """One playlist line, re-expressed for a playlist that has moved.

    The lines in an m3u are relative to the folder holding it, so moving the
    file without touching them changes what every one of them points at. The
    old line is resolved against where it used to live, and written out again
    against where it lives now.

    An absolute line is left exactly as it is — it did not depend on the
    playlist's location and rewriting it could only make it worse.
    """
    if not entry or entry.startswith("#"):
        return entry
    if Path(entry).is_absolute() or "://" in entry:
        return entry

    absolute = Path(os.path.normpath(old_dir / entry))
    return playlist_entry(absolute, playlist_dir=new_dir)


def migrate_playlist_folder() -> int:
    """Carry the playlist across when PLAYLIST_FOLDER has changed.

    Runs at boot. The old hardcoded ``_playlists`` folder is the only source,
    and only files this app writes are moved — ``<PLAYLIST_NAME>.m3u`` and the
    ``musicdrome-scan-NNNN.m3u`` files a much older version left behind. A
    playlist somebody made by hand and dropped in there is not ours to move.

    Every entry is rewritten for the new depth on the way across, because the
    paths inside are relative to the playlist's own folder. Moving the file and
    leaving them alone is the trap this function exists to avoid: the result
    imports as an empty playlist, which looks exactly like not importing at
    all.

    Merging rather than overwriting, for the case where both folders hold a
    playlist — that is two real histories, and picking one to delete is not a
    decision this should make silently.

    Returns the number of files moved. Zero is the normal answer.
    """
    old_dir, new_dir = config.LEGACY_PLAYLIST_DIR, config.PLAYLIST_DIR
    if old_dir == new_dir or not old_dir.is_dir():
        return 0

    # Guarded because this runs inside the boot lifespan: an unreadable old
    # folder is a reason to skip the migration, never a reason for the
    # container to fail to start.
    try:
        ours = sorted(
            {
                *old_dir.glob(f"{config.PLAYLIST_NAME}.m3u"),
                *old_dir.glob("musicdrome-scan-[0-9]*.m3u"),
            }
        )
    except OSError as exc:
        log.warning("could not read the old playlist folder %s: %s", old_dir, exc)
        return 0
    if not ours:
        return 0

    moved = 0
    with _playlist_lock:
        try:
            new_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("could not create the playlist folder %s: %s", new_dir, exc)
            return 0

        for source in ours:
            target = new_dir / source.name
            try:
                entries = _parse_playlist(source)
                seen = playlist_paths(target)
                new_file = not target.exists()
                with target.open("a", encoding="utf-8") as handle:
                    if new_file:
                        handle.write("#EXTM3U\n")
                    for extinf, entry in entries:
                        rewritten = rewrite_entry(entry, old_dir, new_dir)
                        if rewritten in seen:
                            continue
                        seen.add(rewritten)
                        if extinf:
                            handle.write(f"{extinf}\n")
                        handle.write(f"{rewritten}\n")
                source.unlink()
                moved += 1
            except OSError as exc:
                log.warning("could not move %s to %s: %s", source, target, exc)

    # Only when we emptied it. Somebody else's playlists sitting in there are
    # reason enough to leave the folder exactly where it is.
    try:
        if not any(old_dir.iterdir()):
            old_dir.rmdir()
    except OSError as exc:
        log.debug("left %s in place: %s", old_dir, exc)

    if moved:
        log.info(
            "moved %d playlist(s) from %s to %s, rewriting their paths for the new location",
            moved, old_dir, new_dir,
        )
    return moved


def consolidate_scan_playlists() -> int:
    """Fold the old per-scan playlists into the single one and delete them.

    Runs once, at boot, for installs that predate the single playlist. Only
    files this app wrote are touched — the ``musicdrome-scan-NNNN.m3u`` naming
    is matched exactly, inside our own playlist directory — so nothing a person
    made by hand is at risk.
    """
    if not config.PLAYLIST_DIR.is_dir():
        return 0

    # Never the destination itself, however MUSICDROME_PLAYLIST_NAME is set —
    # merging a file into itself and then deleting it would lose the lot.
    old = [
        path
        for path in sorted(config.PLAYLIST_DIR.glob("musicdrome-scan-[0-9]*.m3u"))
        if path != config.PLAYLIST_PATH
    ]
    if not old:
        return 0

    with _playlist_lock:
        playlist = config.PLAYLIST_PATH
        seen = playlist_paths(playlist)
        merged = 0
        try:
            new_file = not playlist.exists()
            with playlist.open("a", encoding="utf-8") as handle:
                if new_file:
                    handle.write("#EXTM3U\n")
                for source in old:
                    for extinf, entry in _parse_playlist(source):
                        if entry in seen:
                            continue
                        seen.add(entry)
                        if extinf:
                            handle.write(f"{extinf}\n")
                        handle.write(f"{entry}\n")
                        merged += 1
        except OSError as exc:
            log.warning("could not merge the old scan playlists: %s", exc)
            return 0

        removed = 0
        for source in old:
            try:
                source.unlink()
                removed += 1
            except OSError as exc:
                log.warning("could not remove %s: %s", source, exc)

    log.info(
        "merged %d track(s) from %d per-scan playlist(s) into %s",
        merged, removed, playlist.name,
    )
    return merged


# ─── Temporary files ───────────────────────────────────────────────────────


def sweep_temp(max_age: int = 3600) -> int:
    """Delete scratch directories left behind by an interrupted download.

    The normal path removes its own workdir in a ``finally``, but a container
    killed mid-download never runs it. Anything older than ``max_age`` cannot
    belong to a live download, so it is safe to remove at boot.
    """
    removed = 0
    if not config.TMP_DIR.is_dir():
        return removed

    cutoff = time.time() - max_age
    for entry in config.TMP_DIR.iterdir():
        if not entry.name.startswith("musicdrome-"):
            continue
        try:
            if entry.stat().st_mtime > cutoff:
                continue
            shutil.rmtree(entry, ignore_errors=True) if entry.is_dir() else entry.unlink()
            removed += 1
        except OSError:
            continue

    if removed:
        log.info("swept %d stale download scratch directories", removed)
    return removed


# ─── Fetching ──────────────────────────────────────────────────────────────


def fetch(download_id: int) -> None:
    """Run one queued download to completion, updating its row as it goes."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT d.*, s.year, s.track_no, s.tags, s.cover_url, s.recording_mbid, "
            "       s.duration AS want_duration "
            "FROM downloads d LEFT JOIN suggestions s ON s.id = d.suggestion_id "
            "WHERE d.id = ?",
            (download_id,),
        ).fetchone()
    if row is None or row["status"] not in {"queued", "downloading"}:
        return

    meta = dict(row)
    _set_status(download_id, "downloading")
    _progress[download_id] = 0

    try:
        # Checked before the download rather than after it. The old check only
        # asked whether the directory existed, which an unwritable mount does,
        # so the failure landed at the final mkdir — having already spent the
        # search, the transfer and the encode on a file that could never be
        # filed. Failing here costs nothing and says what to fix.
        problem = config.music_dir_problem()
        if problem:
            raise DownloadError(problem)

        # A row that already carries a source URL came from a pasted link —
        # the user named the exact recording, so there is nothing to match.
        if meta.get("source_url"):
            candidates = [
                Candidate(
                    url=meta["source_url"],
                    title=meta["title"],
                    artist=meta["artist"],
                    source=meta.get("source") or "url",
                    score=1.0,
                )
            ]
        else:
            candidates = ranked_matches(
                meta["artist"], meta["title"], int(meta["want_duration"] or 0)
            )
            if not candidates:
                raise DownloadError("no confident match on YouTube Music or YouTube")

        candidate, fetched = _download_first_that_works(download_id, candidates, meta)
        path = fetched.path
        append_to_playlist(path, meta)
        _note_download_ok()

        with db.connect() as conn:
            conn.execute(
                "UPDATE downloads SET status = 'done', path = ?, source_url = ?, source = ?, "
                "bytes = ?, duration = ?, source_codec = ?, source_abr = ?, encoded = ?, "
                "progress = 100, error = '', finished_at = ? WHERE id = ?",
                (
                    str(path), candidate.url, candidate.source,
                    path.stat().st_size, candidate.duration,
                    fetched.source_codec, fetched.source_abr, fetched.encoded,
                    db.now(), download_id,
                ),
            )
            if meta["suggestion_id"]:
                conn.execute(
                    "UPDATE suggestions SET status = 'downloaded', decided_at = ? WHERE id = ?",
                    (db.now(), meta["suggestion_id"]),
                )
        log.info("imported %s (%s)", path, provenance(fetched.source_codec,
                                                     fetched.source_abr, fetched.encoded))

    except Exception as exc:
        message = str(exc)
        if is_forbidden(exc):
            message = explain_forbidden(message)
            _note_403()
        message = message[:500]
        log.warning("download %d failed: %s", download_id, message)
        with db.connect() as conn:
            conn.execute(
                "UPDATE downloads SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                (message, db.now(), download_id),
            )
            if meta["suggestion_id"]:
                conn.execute(
                    "UPDATE suggestions SET status = 'failed', error = ? WHERE id = ?",
                    (message, meta["suggestion_id"]),
                )
    finally:
        _progress.pop(download_id, None)


def explain_forbidden(message: str) -> str:
    """Turn a bare 403 into something that names the likely cause.

    "HTTP Error 403: Forbidden" is true and useless. It is almost never the
    track: the search and the metadata call went through on the same
    connection moments earlier, so what was refused is who is asking.
    """
    return (
        f"{message.strip()} — YouTube refused the media fetch. This is the exit "
        f"address, not the track: VPN and datacenter IPs are blocked routinely. "
        f"Route downloads outside the VPN, or give yt-dlp a signed-in identity "
        f"with YTDLP_COOKIES_FILE or YTDLP_PO_TOKEN."
    )


def _note_403() -> None:
    """Count a refusal, and pause the queue once they start arriving in a run."""
    global _403_streak, _403_until

    if config.YTDLP_403_COOLDOWN <= 0 or config.YTDLP_403_STREAK <= 0:
        return

    with _403_lock:
        _403_streak += 1
        if _403_streak < config.YTDLP_403_STREAK:
            return
        _403_streak = 0
        _403_until = time.time() + config.YTDLP_403_COOLDOWN

    log.warning(
        "%d downloads refused in a row — pausing the queue for %d seconds. "
        "Continuing would just convert the rest of the queue into failures.",
        config.YTDLP_403_STREAK, config.YTDLP_403_COOLDOWN,
    )


def _note_download_ok() -> None:
    """A completed download means whatever was refusing us has stopped."""
    global _403_streak
    with _403_lock:
        _403_streak = 0


def _wait_out_cooldown() -> None:
    """Block until the 403 cooldown has expired. Called between downloads."""
    while True:
        with _403_lock:
            remaining = _403_until - time.time()
        if remaining <= 0:
            return
        # Short sleeps so a container stop is not held up by a long pause.
        time.sleep(min(remaining, 5.0))


def _download_first_that_works(
    download_id: int, candidates: list[Candidate], meta: dict[str, Any]
) -> tuple[Candidate, Fetched]:
    """Try each candidate in turn, moving on only when one is refused.

    A 403 is the single failure worth trying a different upload for. Anything
    else — no formats, a bad encode, an unwritable library — will fail exactly
    the same way on the next candidate, so it is raised immediately rather than
    spending three searches to arrive at the same message.
    """
    last: Exception | None = None

    for index, candidate in enumerate(candidates):
        log.info(
            "downloading %s — %s from %s (score %.2f)",
            meta["artist"], meta["title"], candidate.source, candidate.score,
        )
        try:
            return candidate, _download_audio(download_id, candidate, meta)
        except Exception as exc:
            if not is_forbidden(exc):
                raise
            last = exc
            if index + 1 < len(candidates):
                log.info(
                    "refused by YouTube — trying the next candidate (%d of %d)",
                    index + 2, len(candidates),
                )

    raise DownloadError(str(last) if last else "no candidate could be downloaded")


def _download_audio(download_id: int, candidate: Candidate, meta: dict[str, Any]) -> Fetched:
    """yt-dlp into a scratch dir, transcode, tag, then file it."""
    import yt_dlp

    def hook(status: dict) -> None:
        if status.get("status") == "finished":
            _progress[download_id] = 97  # ffmpeg is converting
            return
        if status.get("status") != "downloading":
            return
        total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
        if total:
            _progress[download_id] = min(95, int(status.get("downloaded_bytes", 0) * 95 / total))

    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="musicdrome-", dir=config.TMP_DIR))
    extension = audio_extension()
    try:
        options = _ydl_options(
            outtmpl=str(workdir / "%(id)s.%(ext)s"),
            progress_hooks=[hook],
            postprocessors=[
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": config.AUDIO_FORMAT,
                    # Not a 0-9 VBR value, so yt-dlp passes this to ffmpeg as
                    # -b:a <n>k. Only reached when the source codec differs
                    # from the target: matching codecs are copied through
                    # untouched, which is the whole point of defaulting to the
                    # format YouTube already serves.
                    "preferredquality": config.AUDIO_BITRATE,
                }
            ],
            ffmpeg_location=str(Path(config.FFMPEG_PATH).parent),
        )
        info = _extract_with_retry(yt_dlp, options, candidate, workdir)
        source_codec, source_abr = _source_audio(info)

        produced = sorted(
            (p for p in workdir.iterdir()
             if p.is_file() and p.suffix.lower() == f".{extension}"),
            key=lambda p: p.stat().st_size,
            reverse=True,
        )
        if not produced:
            raise DownloadError(
                f"yt-dlp produced no {extension.upper()} — is ffmpeg installed?"
            )

        source = produced[0]
        tag(source, meta)

        target = target_path(
            meta["artist"], meta["album"], meta["title"], int(meta["track_no"] or 0), extension
        )
        shutil.move(str(source), str(target))

        return Fetched(target, source_codec, source_abr, encoding_of(source_codec, extension))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _extract_with_retry(yt_dlp, options: dict[str, Any], candidate: Candidate, workdir: Path) -> Any:
    """Download one candidate, re-extracting when YouTube answers 403.

    The media URLs yt-dlp hands to the transfer are signed and short-lived, and
    a 403 partway through usually means the one in hand was rejected — the
    player response it came from is minutes old, or the client it was issued
    for has just been throttled. Extracting again produces fresh URLs, which is
    why this retries the whole ``extract_info`` rather than the transfer.

    Only 403s are retried. Everything else fails on the first attempt, as it
    should: a missing format or a broken ffmpeg does not improve with waiting.
    """
    attempts = max(0, config.YTDLP_403_RETRIES) + 1

    for attempt in range(attempts):
        try:
            return _extract_info(yt_dlp, options, candidate.url, download=True)
        except Exception as exc:
            if not is_forbidden(exc) or attempt + 1 >= attempts:
                raise
            delay = 2 * (3 ** attempt)  # 2s, 6s, 18s
            log.info(
                "403 on %s — re-extracting in %ds (attempt %d of %d)",
                candidate.url, delay, attempt + 2, attempts,
            )
            # Partial output from the refused attempt, which would otherwise be
            # picked up as the finished file.
            for leftover in workdir.iterdir():
                if leftover.is_file():
                    leftover.unlink(missing_ok=True)
            time.sleep(delay)


def _set_status(download_id: int, status: str) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE downloads SET status = ? WHERE id = ?", (status, download_id))


# ─── Queue ─────────────────────────────────────────────────────────────────


def downloads_today() -> int:
    since = db.now() - 86400
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM downloads WHERE status = 'done' AND finished_at >= ?",
            (since,),
        ).fetchone()
    return row["n"] if row else 0


def enqueue(suggestion_id: int) -> int | None:
    """Queue one suggestion for download. Returns the download id, or None."""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
        if row is None:
            return None
        existing = conn.execute(
            "SELECT id FROM downloads WHERE suggestion_id = ? AND status IN "
            "('queued', 'downloading', 'done')",
            (suggestion_id,),
        ).fetchone()
        if existing:
            return existing["id"]

        cursor = conn.execute(
            "INSERT INTO downloads (suggestion_id, track_key, artist, title, album, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                suggestion_id, row["track_key"], row["artist"], row["title"],
                row["album"], db.now(),
            ),
        )
        download_id = cursor.lastrowid
        conn.execute(
            "UPDATE suggestions SET status = 'queued', error = '', decided_at = ? WHERE id = ?",
            (db.now(), suggestion_id),
        )

    _queue.put(download_id)
    return download_id


def enqueue_direct(*, artist: str, title: str, album: str = "", url: str = "",
                   source: str = "url") -> int:
    """Queue a track the user named directly, by pasted link or by hand.

    When ``url`` is set the download skips matching entirely — the link already
    identifies the exact recording, so second-guessing it would be wrong.
    """
    with db.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO downloads (track_key, artist, title, album, source_url, source, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (track_key(artist, title), artist, title, album, url, source, db.now()),
        )
        download_id = cursor.lastrowid

    _queue.put(download_id)
    return download_id


def auto_enqueue(scan_id: int | None = None) -> int:
    """Queue high-confidence suggestions, if auto-download is switched on.

    The daily cap counts finished downloads in the last 24 hours, so a runaway
    scan cannot fill a disk overnight — it just stops queueing.
    """
    settings = db.get_settings()
    if not settings["auto_download"]:
        return 0

    remaining = int(settings["daily_download_cap"]) - downloads_today()
    if remaining <= 0:
        log.info("daily download cap of %s reached", settings["daily_download_cap"])
        return 0

    query = (
        "SELECT id FROM suggestions WHERE status = 'new' AND match >= ? "
        + ("AND scan_id = ? " if scan_id else "")
        + "ORDER BY match DESC LIMIT ?"
    )
    params: list[Any] = [int(settings["auto_download_threshold"])]
    if scan_id:
        params.append(scan_id)
    params.append(remaining)

    with db.connect() as conn:
        ids = [row["id"] for row in conn.execute(query, params)]

    queued = sum(1 for suggestion_id in ids if enqueue(suggestion_id))
    if queued:
        log.info("auto-queued %d downloads above %s%%", queued, settings["auto_download_threshold"])
    return queued


def active() -> list[dict[str, Any]]:
    """In-flight downloads, for the UI's progress poll."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, artist, title, status FROM downloads "
            "WHERE status IN ('queued', 'downloading') ORDER BY id"
        ).fetchall()
    return [{**dict(row), "progress": _progress.get(row["id"], 0)} for row in rows]


def _worker() -> None:
    while True:
        download_id = _queue.get()
        try:
            # Held here rather than inside fetch() so a paused queue leaves the
            # row 'queued' — a download that has not started has not failed.
            _wait_out_cooldown()
            fetch(download_id)
        except Exception:
            log.exception("download worker crashed on %s", download_id)
        finally:
            _queue.task_done()


def start_workers() -> None:
    """Start the pool, sweep old scratch files and requeue interrupted work."""
    if _workers:
        return

    sweep_temp()

    with db.connect() as conn:
        conn.execute("UPDATE downloads SET status = 'queued' WHERE status = 'downloading'")
        pending = [
            row["id"]
            for row in conn.execute("SELECT id FROM downloads WHERE status = 'queued' ORDER BY id")
        ]

    for index in range(max(1, config.DOWNLOAD_CONCURRENCY)):
        thread = threading.Thread(target=_worker, name=f"musicdrome-download-{index}", daemon=True)
        thread.start()
        _workers.append(thread)

    for download_id in pending:
        _queue.put(download_id)
    if pending:
        log.info("requeued %d interrupted downloads", len(pending))


def retry(download_id: int) -> bool:
    """Put a failed download back on the queue."""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM downloads WHERE id = ?", (download_id,)).fetchone()
        if row is None or row["status"] not in {"failed", "done"}:
            return False
        _reset(conn, row)
    _queue.put(download_id)
    return True


def retry_all_failed() -> int:
    """Requeue every failed download. Returns how many were requeued.

    Worth having as one action: failures usually share a cause — a stale
    yt-dlp, a YouTube change, a network blip — so when the cause is fixed they
    all become downloadable at the same moment.
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM downloads WHERE status = 'failed' ORDER BY id"
        ).fetchall()
        for row in rows:
            _reset(conn, row)

    for row in rows:
        _queue.put(row["id"])
    if rows:
        log.info("requeued %d failed downloads", len(rows))
    return len(rows)


def _reset(conn, row) -> None:
    """Return one download row, and its suggestion, to the queued state."""
    conn.execute(
        "UPDATE downloads SET status = 'queued', error = '', progress = 0, finished_at = NULL "
        "WHERE id = ?",
        (row["id"],),
    )
    if row["suggestion_id"]:
        conn.execute(
            "UPDATE suggestions SET status = 'queued', error = '' WHERE id = ?",
            (row["suggestion_id"],),
        )


def remove(download_id: int, delete_file: bool = False) -> bool:
    """Forget a download, optionally deleting the file it produced."""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM downloads WHERE id = ?", (download_id,)).fetchone()
        if row is None:
            return False
        if delete_file and row["path"]:
            path = Path(row["path"])
            try:
                # Only ever delete inside MUSIC_DIR, whatever the row claims.
                path.resolve().relative_to(config.MUSIC_DIR.resolve())
                path.unlink(missing_ok=True)
            except (ValueError, OSError) as exc:
                log.warning("refusing to delete %s: %s", path, exc)
        conn.execute("DELETE FROM downloads WHERE id = ?", (download_id,))
        if row["suggestion_id"]:
            conn.execute(
                "UPDATE suggestions SET status = 'new', error = '' WHERE id = ?",
                (row["suggestion_id"],),
            )
    return True


def track_is_downloaded(artist: str, title: str) -> bool:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM downloads WHERE track_key = ? AND status = 'done' LIMIT 1",
            (track_key(artist, title),),
        ).fetchone()
    return row is not None
