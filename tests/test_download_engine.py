"""yt-dlp configuration, format handling, temp sweeping and retry-all.

The yt-dlp option tests are the important ones: whether downloads work at all
comes down to the JS runtime and to *not* pinning a stale player-client list,
and both fail with messages that name neither yt-dlp nor the real cause.
"""

import time

import pytest

from app import config, db, download


# ─── yt-dlp options ────────────────────────────────────────────────────────


def test_player_clients_are_left_to_yt_dlp_by_default():
    """A pin here freezes yt-dlp's choice at the moment it was written.

    This is the regression that produced yt-dlp/yt-dlp#12482 in the logs: the
    list led with ios/android long after YouTube moved both to SABR-only, so
    every video tried two dead clients first.
    """
    youtube = download._ydl_options()["extractor_args"]["youtube"]
    assert "player_client" not in youtube


def test_player_clients_are_overridable(monkeypatch):
    monkeypatch.setattr(config, "YTDLP_PLAYER_CLIENTS", "tv,web")
    assert download._ydl_options()["extractor_args"]["youtube"]["player_client"] == ["tv", "web"]


def test_a_js_runtime_and_ejs_components_are_enabled():
    """Without both, every client is either SABR-only or unsolvable."""
    options = download._ydl_options()
    assert options["js_runtimes"] == {"deno": {}}
    assert "ejs:github" in options["remote_components"]


def test_the_default_runtime_is_one_yt_dlp_actually_accepts():
    """The bug this pins down: Debian's Node is below yt-dlp's floor of 22.

    Naming a runtime is not the same as having a usable one. yt-dlp treats a
    version below its minimum exactly as it treats a missing binary — it drops
    to the JS-less clients and YouTube answers those with 403 — so the image
    shipped `nodejs` for months without a single download ever using it.
    """
    yt_dlp = pytest.importorskip("yt_dlp")

    minimums = {
        name: runtime.MIN_SUPPORTED_VERSION
        for name, runtime in yt_dlp.globals.supported_js_runtimes.value.items()
    }
    for name in config.js_runtimes():
        assert name in minimums, f"{name} is not a runtime yt-dlp supports"


# ─── JS runtime health ─────────────────────────────────────────────────────


def _fake_ydl(monkeypatch, runtimes):
    """Stand in for yt_dlp.YoutubeDL, reporting the runtimes we hand it."""
    import yt_dlp

    class FakeYDL:
        def __init__(self, params):
            self.params = params

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        @property
        def _js_runtimes(self):
            return runtimes

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)


def _runtime(name, version, supported):
    from types import SimpleNamespace

    info = SimpleNamespace(name=name, version=version, supported=supported)
    return SimpleNamespace(info=info)


def test_a_working_runtime_reports_no_problem(monkeypatch):
    _fake_ydl(monkeypatch, {"deno": _runtime("deno", "2.9.5", True)})
    assert download.js_runtime_problem() == ""


def test_a_runtime_below_the_minimum_is_reported(monkeypatch):
    """Debian bookworm's Node 18, precisely — present, detected, useless."""
    _fake_ydl(monkeypatch, {"node": _runtime("node", "18.20.4", False)})

    problem = download.js_runtime_problem()
    assert "18.20.4" in problem
    assert "older than yt-dlp accepts" in problem
    assert "403" in problem, "the message must connect to the symptom people see"


def test_a_missing_runtime_is_reported(monkeypatch):
    _fake_ydl(monkeypatch, {"deno": None})

    problem = download.js_runtime_problem()
    assert "deno was not found" in problem


def test_one_working_runtime_is_enough(monkeypatch):
    _fake_ydl(monkeypatch, {
        "deno": _runtime("deno", "2.9.5", True),
        "node": _runtime("node", "18.20.4", False),
    })
    assert download.js_runtime_problem() == ""


def test_an_unrecognised_runtime_name_is_named_back(monkeypatch):
    """"nodejs" is not "node", and yt-dlp says so by silently dropping it.

    It drops unknown names by popping them out of the dict it was handed, so
    reading that dict afterwards to build the message reports nothing at all.
    """
    monkeypatch.setattr(config, "YTDLP_JS_RUNTIMES", "nodejs")
    _fake_ydl(monkeypatch, {})

    problem = download.js_runtime_problem()
    assert "nodejs" in problem, "the message must name what was configured"


def test_deliberately_disabling_the_runtime_is_not_a_problem(monkeypatch):
    monkeypatch.setattr(config, "YTDLP_JS_RUNTIMES", "")
    assert download.js_runtime_problem() == ""


def test_the_check_never_takes_the_app_down(monkeypatch):
    """It reads a private yt-dlp attribute, which is allowed to disappear."""
    import yt_dlp

    def explode(*args, **kwargs):
        raise RuntimeError("yt-dlp changed shape")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", explode)
    assert download.js_runtime_problem() == ""


def test_js_runtime_can_be_disabled(monkeypatch):
    monkeypatch.setattr(config, "YTDLP_JS_RUNTIMES", "")
    assert download._ydl_options()["js_runtimes"] == {}


def test_retries_are_generous_enough_for_a_flaky_cdn():
    options = download._ydl_options()
    assert options["retries"] >= 10
    assert options["fragment_retries"] >= 10
    assert options["extractor_retries"] >= 3


def test_requests_are_paced_to_avoid_rate_limiting():
    assert download._ydl_options()["sleep_interval_requests"] >= 1


def test_optional_escape_hatches_are_absent_unless_configured():
    options = download._ydl_options()
    for key in ("cookiefile", "cookiesfrombrowser", "proxy", "source_address", "ratelimit"):
        assert key not in options
    assert "po_token" not in options["extractor_args"]["youtube"]


def test_po_token_is_passed_through(monkeypatch):
    monkeypatch.setattr(config, "YTDLP_PO_TOKEN", "mweb.gvs+ABC, web.gvs+XYZ")
    tokens = download._ydl_options()["extractor_args"]["youtube"]["po_token"]
    assert tokens == ["mweb.gvs+ABC", "web.gvs+XYZ"]


def test_force_ipv4_binds_the_source_address(monkeypatch):
    monkeypatch.setattr(config, "YTDLP_FORCE_IPV4", True)
    assert download._ydl_options()["source_address"] == "0.0.0.0"


def test_cookies_from_browser_accepts_a_profile(monkeypatch):
    monkeypatch.setattr(config, "YTDLP_COOKIES_FROM_BROWSER", "chrome:Default")
    assert download._ydl_options()["cookiesfrombrowser"] == ("chrome", "Default")


def test_overrides_win():
    assert download._ydl_options(retries=1)["retries"] == 1


# ─── Harmless-warning suppression ──────────────────────────────────────────


def test_known_harmless_warnings_are_swallowed(caplog):
    download._YtdlpLogger.warning("... requires a GVS PO Token which was not provided")
    assert not caplog.records


def test_the_sabr_warning_is_swallowed(caplog):
    """One line per client per video, on a scan of forty tracks. It buried
    everything that actually mattered — including the permission error that was
    the real reason nothing was being filed."""
    download._YtdlpLogger.warning(
        "[youtube] p8YifQtbed4: Some android client https formats have been skipped as "
        "they are missing a URL. YouTube may have enabled the SABR-only streaming "
        "experiment for the current session. See  "
        "https://github.com/yt-dlp/yt-dlp/issues/12482  for more details"
    )
    assert not caplog.records


def test_real_warnings_still_surface(caplog):
    with caplog.at_level("WARNING"):
        download._YtdlpLogger.warning("something genuinely wrong")
    assert any("something genuinely wrong" in r.message for r in caplog.records)


# ─── Search ────────────────────────────────────────────────────────────────


def test_search_is_flat_so_one_match_is_not_five_extractions(monkeypatch):
    """Resolving every hit in full is what made a search take fifteen seconds
    and emit a screenful of client warnings."""
    captured = {}

    class FakeYDL:
        def __init__(self, options):
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, *a, **k):
            return {"entries": []}

    import sys
    import types

    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = FakeYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)

    download.search_youtube("Radiohead", "Karma Police")
    assert captured["extract_flat"] == "in_playlist"


@pytest.mark.parametrize(
    "entry,expected",
    [
        ({"uploader": "MEDUZA - Topic"}, "MEDUZA"),
        ({"uploader": "Tinlicker - topic"}, "Tinlicker"),
        ({"channel": "BUNT. - Topic"}, "BUNT."),
        ({"artist": "Portishead", "uploader": "Some Reuploader"}, "Portishead"),
        ({"uploader": "Topic Records"}, "Topic Records"),
        ({}, ""),
    ],
)
def test_topic_channels_are_credited_to_the_artist(entry, expected):
    """YouTube's auto-generated "<Artist> - Topic" channels are the only
    catalogue metadata a plain search has. Scoring refuses to download anything
    it cannot attribute, so throwing this away turned real matches into
    "no confident match"."""
    assert download._channel_artist(entry) == expected


# ─── Audio format ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "fmt,extension", [("mp3", "mp3"), ("m4a", "m4a"), ("opus", "opus"),
                      ("flac", "flac"), ("vorbis", "ogg")],
)
def test_extension_follows_the_configured_format(monkeypatch, fmt, extension):
    monkeypatch.setattr(config, "AUDIO_FORMAT", fmt)
    assert download.audio_extension() == extension


def test_target_path_uses_the_configured_extension(monkeypatch):
    monkeypatch.setattr(config, "AUDIO_FORMAT", "opus")
    assert download.target_path("A", "B", "C", 1).name == "01 - C.opus"


def test_target_path_extension_can_be_overridden():
    assert download.target_path("A", "B", "C", 0, "flac").name == "C.flac"


def test_the_default_format_is_what_youtube_already_serves(monkeypatch):
    """Opus, so ffmpeg copies YouTube's own stream through instead of spending
    a second lossy encode on an already-lossy source."""
    assert config.AUDIO_FORMAT == "opus"
    assert download.format_sort() == ["acodec:opus"]


@pytest.mark.parametrize(
    "fmt,sort",
    [
        ("opus", ["acodec:opus"]),
        ("m4a", ["acodec:aac"]),
        ("vorbis", ["acodec:vorbis"]),
        # Nothing YouTube serves is MP3 or FLAC, so there is nothing to prefer
        # and yt-dlp's own ordering — highest bitrate — is left alone.
        ("mp3", []),
        ("flac", []),
    ],
)
def test_the_source_stream_that_needs_no_re_encode_is_preferred(monkeypatch, fmt, sort):
    monkeypatch.setattr(config, "AUDIO_FORMAT", fmt)
    assert download.format_sort() == sort


def test_the_format_preference_reaches_yt_dlp(monkeypatch):
    monkeypatch.setattr(config, "AUDIO_FORMAT", "opus")
    assert download._ydl_options()["format_sort"] == ["acodec:opus"]


def test_no_preference_is_sent_when_there_is_nothing_to_prefer(monkeypatch):
    monkeypatch.setattr(config, "AUDIO_FORMAT", "mp3")
    assert "format_sort" not in download._ydl_options()


# ─── Cover art ─────────────────────────────────────────────────────────────


def test_ogg_cover_art_is_a_flac_picture_block(monkeypatch):
    """Ogg has no picture field. Every player reads the same convention: a FLAC
    picture block, base64-encoded, in a METADATA_BLOCK_PICTURE comment."""
    import base64

    from mutagen.flac import Picture

    cover = b"\xff\xd8\xff\xe0" + b"payload"
    picture = Picture(base64.b64decode(download._ogg_picture(cover)))

    assert picture.data == cover
    assert picture.mime == "image/jpeg"
    assert picture.type == 3  # front cover


def test_a_png_cover_is_declared_as_a_png():
    import base64

    from mutagen.flac import Picture

    cover = b"\x89PNG\r\n\x1a\n" + b"payload"
    assert Picture(base64.b64decode(download._ogg_picture(cover))).mime == "image/png"


class _FakeAudio(dict):
    """Enough of a mutagen file for :func:`app.download.tag` to write through."""

    def __init__(self):
        super().__init__()
        self.tags = self
        self.saves = 0

    def add_tags(self):
        pass

    def save(self, *args, **kwargs):
        self.saves += 1


def test_an_opus_download_is_tagged_and_gets_its_artwork(monkeypatch, tmp_path):
    """Without this an Opus library is a library with no artwork in it — the
    cover branch only knew about MP3, FLAC and MP4."""
    import mutagen

    audio = _FakeAudio()
    monkeypatch.setattr(mutagen, "File", lambda *a, **k: audio)
    monkeypatch.setattr(download, "_fetch_cover", lambda url: b"\xff\xd8\xff\xe0cover")

    download.tag(
        tmp_path / "01 - Song.opus",
        {"artist": "A", "title": "Song", "album": "Album", "year": "2019",
         "tags": "electronic,trance", "cover_url": "http://example/cover.jpg"},
    )

    assert audio["artist"] == "A"
    assert audio["genre"] == "electronic"
    assert "metadata_block_picture" in audio
    assert audio.saves == 2  # fields, then the picture


def test_a_format_without_cover_support_is_left_alone(monkeypatch, tmp_path):
    import mutagen

    audio = _FakeAudio()
    monkeypatch.setattr(mutagen, "File", lambda *a, **k: audio)
    monkeypatch.setattr(download, "_fetch_cover", lambda url: b"\xff\xd8\xff\xe0cover")

    download.tag(tmp_path / "01 - Song.wav", {"artist": "A", "cover_url": "http://x"})

    assert "metadata_block_picture" not in audio


# ─── Temp sweeping ─────────────────────────────────────────────────────────


def test_sweep_removes_stale_scratch_directories():
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    stale = config.TMP_DIR / "musicdrome-stale"
    stale.mkdir(exist_ok=True)
    (stale / "part.webm").write_bytes(b"junk")
    old = time.time() - 7200
    import os

    os.utime(stale, (old, old))

    assert download.sweep_temp() == 1
    assert not stale.exists()


def test_sweep_leaves_a_live_download_alone():
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    live = config.TMP_DIR / "musicdrome-live"
    live.mkdir(exist_ok=True)

    assert download.sweep_temp() == 0
    assert live.exists()
    live.rmdir()


def test_sweep_ignores_files_it_does_not_own():
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    theirs = config.TMP_DIR / "someone-elses-file"
    theirs.write_bytes(b"not ours")
    import os

    old = time.time() - 7200
    os.utime(theirs, (old, old))

    assert download.sweep_temp() == 0
    assert theirs.exists()
    theirs.unlink()


def test_sweep_on_a_missing_directory_is_a_no_op(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TMP_DIR", tmp_path / "nope")
    assert download.sweep_temp() == 0


# ─── Retry all ─────────────────────────────────────────────────────────────


def fail_download(download_id: int) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE downloads SET status = 'failed', error = 'boom', finished_at = ? WHERE id = ?",
            (db.now(), download_id),
        )


def test_retry_all_requeues_every_failure(suggestion):
    ids = [download.enqueue(suggestion(f"Artist {i}", f"Track {i}")) for i in range(3)]
    for download_id in ids:
        fail_download(download_id)

    assert download.retry_all_failed() == 3
    with db.connect() as conn:
        statuses = {row["status"] for row in conn.execute("SELECT status FROM downloads")}
        errors = {row["error"] for row in conn.execute("SELECT error FROM downloads")}
    assert statuses == {"queued"}
    assert errors == {""}


def test_retry_all_returns_the_suggestions_to_queued(suggestion):
    suggestion_id = suggestion("A", "One")
    fail_download(download.enqueue(suggestion_id))

    download.retry_all_failed()
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
    assert row["status"] == "queued"


def test_retry_all_leaves_completed_downloads_alone(suggestion):
    done = download.enqueue(suggestion("A", "Done"))
    with db.connect() as conn:
        conn.execute("UPDATE downloads SET status = 'done' WHERE id = ?", (done,))
    fail_download(download.enqueue(suggestion("B", "Failed")))

    assert download.retry_all_failed() == 1
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM downloads WHERE id = ?", (done,)).fetchone()
    assert row["status"] == "done"


def test_retry_all_with_nothing_failed_is_zero():
    assert download.retry_all_failed() == 0


# ─── Direct downloads ──────────────────────────────────────────────────────


def test_enqueue_direct_stores_the_url_so_matching_is_skipped():
    download_id = download.enqueue_direct(
        artist="Boards of Canada", title="Roygbiv",
        url="https://music.youtube.com/watch?v=abc", source="ytmusic",
    )
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM downloads WHERE id = ?", (download_id,)).fetchone()
    assert row["source_url"].endswith("v=abc")
    assert row["suggestion_id"] is None
    assert row["status"] == "queued"


def test_a_direct_download_without_a_url_still_gets_matched():
    """A Spotify link carries metadata but no downloadable audio."""
    download_id = download.enqueue_direct(artist="Portishead", title="Glory Box")
    with db.connect() as conn:
        row = conn.execute("SELECT source_url FROM downloads WHERE id = ?", (download_id,)).fetchone()
    assert row["source_url"] == ""


# ─── HTTP 403 ──────────────────────────────────────────────────────────────
#
# The failure that produced all of this: search and metadata succeed, the media
# fetch comes back 403, and the queue then converts every remaining track into
# a failure at the speed it can dequeue them.


@pytest.fixture(autouse=True)
def no_lingering_cooldown():
    """A test that trips the cooldown must not stall the next one."""
    yield
    download._403_until = 0.0
    download._403_streak = 0


class FakeYdl:
    """A yt-dlp stand-in that fails a set number of times, then succeeds."""

    def __init__(self, failures: int, error: str = "HTTP Error 403: Forbidden"):
        self.remaining = failures
        self.error = error
        self.calls = 0

    def YoutubeDL(self, options):  # noqa: N802 - mirrors yt_dlp's own name
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True):
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise RuntimeError(self.error)
        return {"id": "abc"}


def test_a_403_is_retried_against_freshly_extracted_urls(tmp_path, monkeypatch):
    """The signed media URL goes stale; re-extracting produces a new one."""
    monkeypatch.setattr(config, "YTDLP_403_RETRIES", 2)
    fake = FakeYdl(failures=1)
    sleeps = []
    monkeypatch.setattr(download.time, "sleep", sleeps.append)

    download._extract_with_retry(
        fake, {}, download.Candidate(url="u", title="t"), tmp_path
    )

    assert fake.calls == 2
    assert sleeps == [2]


def test_retries_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "YTDLP_403_RETRIES", 2)
    monkeypatch.setattr(download.time, "sleep", lambda seconds: None)
    fake = FakeYdl(failures=99)

    with pytest.raises(RuntimeError, match="403"):
        download._extract_with_retry(
            fake, {}, download.Candidate(url="u", title="t"), tmp_path
        )
    assert fake.calls == 3  # the first attempt plus two retries


def test_anything_other_than_a_403_fails_immediately(tmp_path, monkeypatch):
    """A missing format or a broken ffmpeg does not improve with waiting."""
    monkeypatch.setattr(config, "YTDLP_403_RETRIES", 2)
    fake = FakeYdl(failures=99, error="Requested format is not available")

    with pytest.raises(RuntimeError):
        download._extract_with_retry(
            fake, {}, download.Candidate(url="u", title="t"), tmp_path
        )
    assert fake.calls == 1


def test_partial_output_is_cleared_between_attempts(tmp_path, monkeypatch):
    """Otherwise the half-file left by the refused attempt looks finished."""
    monkeypatch.setattr(config, "YTDLP_403_RETRIES", 1)
    monkeypatch.setattr(download.time, "sleep", lambda seconds: None)
    (tmp_path / "abc.mp3").write_bytes(b"half a song")

    download._extract_with_retry(
        FakeYdl(failures=1), {}, download.Candidate(url="u", title="t"), tmp_path
    )
    assert not (tmp_path / "abc.mp3").exists()


def test_a_refused_candidate_falls_through_to_the_next(monkeypatch):
    """A different upload of the same track is often served without complaint."""
    tried = []

    def attempt(download_id, candidate, meta):
        tried.append(candidate.url)
        if candidate.url == "first":
            raise download.DownloadError("HTTP Error 403: Forbidden")
        return download.Path("/music/ok.mp3")

    monkeypatch.setattr(download, "_download_audio", attempt)
    candidates = [
        download.Candidate(url="first", title="t"),
        download.Candidate(url="second", title="t"),
    ]

    chosen, path = download._download_first_that_works(
        1, candidates, {"artist": "A", "title": "T"}
    )
    assert tried == ["first", "second"]
    assert chosen.url == "second"
    assert str(path) == "/music/ok.mp3"


def test_a_failure_that_is_not_a_403_does_not_try_other_candidates(monkeypatch):
    tried = []

    def attempt(download_id, candidate, meta):
        tried.append(candidate.url)
        raise download.DownloadError("ffmpeg is not installed")

    monkeypatch.setattr(download, "_download_audio", attempt)
    candidates = [
        download.Candidate(url="first", title="t"),
        download.Candidate(url="second", title="t"),
    ]

    with pytest.raises(download.DownloadError, match="ffmpeg"):
        download._download_first_that_works(1, candidates, {"artist": "A", "title": "T"})
    assert tried == ["first"]


def test_a_streak_of_403s_pauses_the_queue(monkeypatch):
    monkeypatch.setattr(config, "YTDLP_403_STREAK", 3)
    monkeypatch.setattr(config, "YTDLP_403_COOLDOWN", 300)

    download._note_403()
    download._note_403()
    assert download._403_until == 0.0  # two is not a pattern

    download._note_403()
    assert download._403_until > time.time()


def test_a_completed_download_clears_the_streak(monkeypatch):
    monkeypatch.setattr(config, "YTDLP_403_STREAK", 3)
    monkeypatch.setattr(config, "YTDLP_403_COOLDOWN", 300)

    download._note_403()
    download._note_403()
    download._note_download_ok()
    download._note_403()

    assert download._403_until == 0.0


def test_the_cooldown_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(config, "YTDLP_403_COOLDOWN", 0)

    for _ in range(10):
        download._note_403()
    assert download._403_until == 0.0


def test_the_403_message_names_the_likely_cause():
    message = download.explain_forbidden("ERROR: unable to download video data: HTTP Error 403")
    assert "VPN" in message
    assert "YTDLP_COOKIES_FILE" in message


@pytest.mark.parametrize("message", [
    "ERROR: unable to download video data: HTTP Error 403: Forbidden",
    "fragment 1 not found, unable to continue: HTTP Error 403",
    "HTTP Error 403: Forbidden",
])
def test_refusals_are_recognised(message):
    assert download.is_forbidden(message)


@pytest.mark.parametrize("message", [
    "Requested format is not available",
    "no confident match on YouTube Music or YouTube",
    "HTTP Error 404: Not Found",
])
def test_other_failures_are_not_mistaken_for_refusals(message):
    assert not download.is_forbidden(message)


# ─── Browser impersonation ─────────────────────────────────────────────────


def test_impersonation_is_omitted_when_it_is_unavailable(monkeypatch):
    """Asking for a target yt-dlp cannot provide fails every single request."""
    monkeypatch.setattr(download, "_impersonate_cache", download._UNSET)
    monkeypatch.setattr(download, "impersonate_target", lambda: None)
    assert "impersonate" not in download._ydl_options()


def test_impersonation_is_passed_through_when_available(monkeypatch):
    monkeypatch.setattr(download, "impersonate_target", lambda: "chrome-target")
    assert download._ydl_options()["impersonate"] == "chrome-target"


def test_impersonation_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(config, "YTDLP_IMPERSONATE", "")
    monkeypatch.setattr(download, "_impersonate_cache", download._UNSET)
    assert download.impersonate_target() is None
    assert "off" in download.impersonation_status()


class _FakeYoutubeDL:
    """Stand-in for yt_dlp.YoutubeDL that records how it was constructed."""

    def __init__(self, options, *, fail_with=None):
        self.options = options
        self._fail_with = fail_with

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def extract_info(self, url, download=False):
        if self._fail_with:
            raise RuntimeError(self._fail_with)
        return {"url": url}


def _fake_yt_dlp(*, fail_while_impersonating=""):
    """A yt_dlp module whose requests fail only while a target is requested."""
    calls = []

    class Module:
        @staticmethod
        def YoutubeDL(options):
            calls.append(dict(options))
            failure = fail_while_impersonating if "impersonate" in options else ""
            return _FakeYoutubeDL(options, fail_with=failure)

    return Module, calls


# The exact text yt-dlp raises, from the Musicdrome logs that prompted this.
UNAVAILABLE = (
    'Impersonate target "chrome" is not available. Use --list-impersonate-targets '
    "to see available targets. You may be missing dependencies required to support "
    "this target."
)


def test_an_unavailable_target_is_recognised():
    assert download.is_impersonation_unavailable(UNAVAILABLE)
    assert download.is_impersonation_unavailable(f"ERROR: {UNAVAILABLE}")


@pytest.mark.parametrize("message", [
    "HTTP Error 403: Forbidden",
    "Requested format is not available",
    # A video title, quoted back by yt-dlp — not a report about impersonation.
    "Requested format is not available: Impersonate Target (Official Video)",
])
def test_other_failures_are_not_mistaken_for_missing_impersonation(message):
    assert not download.is_impersonation_unavailable(message)


def test_extraction_retries_without_impersonation_when_the_target_is_refused(monkeypatch):
    """One failed request, not a queue full of them."""
    monkeypatch.setattr(download, "_impersonate_cache", "chrome-target")
    yt_dlp, calls = _fake_yt_dlp(fail_while_impersonating=UNAVAILABLE)
    options = {"format": "bestaudio/best", "impersonate": "chrome-target"}

    assert download._extract_info(yt_dlp, options, "https://y.t/x", download=True)

    assert len(calls) == 2
    assert "impersonate" in calls[0]
    assert "impersonate" not in calls[1]
    # The caller's options are cleaned too, so the 403 retry loop that reuses
    # them does not put the dead target back on the wire.
    assert "impersonate" not in options
    # And every later request skips the doomed first attempt entirely.
    assert download.impersonate_target() is None


def test_other_extraction_failures_are_not_retried(monkeypatch):
    monkeypatch.setattr(download, "_impersonate_cache", "chrome-target")
    yt_dlp, calls = _fake_yt_dlp(fail_while_impersonating="HTTP Error 403: Forbidden")

    with pytest.raises(RuntimeError):
        download._extract_info(
            yt_dlp, {"impersonate": "chrome-target"}, "https://y.t/x", download=True
        )

    assert len(calls) == 1
    # A 403 is handled by re-extraction upstream; impersonation is not to blame
    # for it and must survive.
    assert download.impersonate_target() == "chrome-target"


def test_no_available_targets_means_impersonation_is_off(monkeypatch):
    """The regression that broke every download.

    curl_cffi imports cleanly, yt-dlp declines to load it because the version
    is outside the range it was built against, and the target list comes back
    empty. Read as "inconclusive" that cached a target nothing could serve, and
    the boot log cheerfully announced it while every request failed.
    """
    monkeypatch.setattr(config, "YTDLP_IMPERSONATE", "chrome")
    monkeypatch.setattr(download, "_impersonate_cache", download._UNSET)
    monkeypatch.setattr(download, "_impersonate_reason", "")

    class Ydl:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def _get_available_impersonate_targets(self):
            return []

    monkeypatch.setattr(download, "_no_handler_reason", lambda module: "curl_cffi 0.16.0 is too new")

    import yt_dlp

    monkeypatch.setattr(yt_dlp, "YoutubeDL", lambda options: Ydl())

    assert download.impersonate_target() is None
    assert "impersonate" not in download._ydl_options()
    # And the boot line says why, rather than claiming to impersonate Chrome.
    status = download.impersonation_status()
    assert status.startswith("off")
    assert "0.16.0" in status


def test_a_track_called_forbidden_is_not_mistaken_for_a_refusal():
    """yt-dlp quotes video titles back in its errors."""
    assert not download.is_forbidden(
        "ERROR: Requested format is not available: Forbidden Fruit (Official Video)"
    )
