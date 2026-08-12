"""The music directory, and why a download failed.

The failure this covers is a specific one seen in the wild: /music mounted from
a share owned by another uid. Every layer reported success — the container
booted, scans ran, matches were found, yt-dlp downloaded and ffmpeg encoded —
and then the very last step, creating the artist folder, raised EACCES. The
bandwidth was already spent and the log said only "Permission denied".
"""

import os

import pytest

from app import config, download


@pytest.fixture
def music_dir(tmp_path, monkeypatch):
    directory = tmp_path / "music"
    directory.mkdir()
    monkeypatch.setattr(config, "MUSIC_DIR", directory)
    return directory


def test_a_writable_directory_reports_no_problem(music_dir):
    assert config.music_dir_problem() == ""


def test_a_missing_directory_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MUSIC_DIR", tmp_path / "nope")
    assert "does not exist" in config.music_dir_problem()


def test_a_file_where_a_directory_should_be_is_reported(tmp_path, monkeypatch):
    path = tmp_path / "music"
    path.write_text("not a directory")
    monkeypatch.setattr(config, "MUSIC_DIR", path)
    assert "not a directory" in config.music_dir_problem()


@pytest.mark.skipif(os.getuid() == 0, reason="root can write to anything")
def test_an_unwritable_directory_names_the_uids(music_dir):
    """`exists()` is true here, which is exactly why the old check missed it."""
    music_dir.chmod(0o500)
    try:
        problem = config.music_dir_problem()
    finally:
        music_dir.chmod(0o700)

    assert "not writable" in problem
    assert "PUID/PGID" in problem, "the message must say how to fix it"
    assert str(os.getuid()) in problem, "the message must name the uid we are running as"


@pytest.mark.skipif(os.getuid() == 0, reason="root can write to anything")
def test_a_download_fails_before_spending_bandwidth(music_dir, monkeypatch, suggestion):
    """The check has to happen before the transfer, not after the encode."""
    downloaded = []
    monkeypatch.setattr(
        download, "_download_audio", lambda *a, **k: downloaded.append(1)
    )
    monkeypatch.setattr(
        download, "best_match",
        lambda *a, **k: download.Candidate(url="u", title="t", artist="a", score=1.0),
    )

    suggestion_id = suggestion("Portishead", "Glory Box")
    download_id = download.enqueue(suggestion_id)

    music_dir.chmod(0o500)
    try:
        download.fetch(download_id)
    finally:
        music_dir.chmod(0o700)

    assert not downloaded, "nothing should be downloaded into a directory we cannot write"

    from app import db

    with db.connect() as conn:
        row = conn.execute(
            "SELECT status, error FROM downloads WHERE id = ?", (download_id,)
        ).fetchone()
    assert row["status"] == "failed"
    assert "not writable" in row["error"]
