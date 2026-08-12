"""yt-dlp configuration, format handling, temp sweeping and retry-all.

The yt-dlp option tests are the important ones: the player-client list is what
makes downloads work at all, and its absence fails with messages that name
neither yt-dlp nor the client ("Requested format is not available").
"""

import time

import pytest

from app import config, db, download


# ─── yt-dlp options ────────────────────────────────────────────────────────


def test_player_clients_are_always_set_and_ios_leads():
    clients = download._ydl_options()["extractor_args"]["youtube"]["player_client"]
    assert clients[:2] == ["ios", "android"], (
        "ios/android must lead: they serve audio without a JS runtime, "
        "which the container does not have"
    )


def test_player_clients_are_overridable(monkeypatch):
    monkeypatch.setattr(config, "YTDLP_PLAYER_CLIENTS", "tv,web")
    assert download._ydl_options()["extractor_args"]["youtube"]["player_client"] == ["tv", "web"]


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


def test_real_warnings_still_surface(caplog):
    with caplog.at_level("WARNING"):
        download._YtdlpLogger.warning("something genuinely wrong")
    assert any("something genuinely wrong" in r.message for r in caplog.records)


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
