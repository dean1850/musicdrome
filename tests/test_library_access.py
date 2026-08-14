"""The music directory, and why a download failed.

The failure this covers is a specific one seen in the wild: /music mounted from
a share owned by another uid. Every layer reported success — the container
booted, scans ran, matches were found, yt-dlp downloaded and ffmpeg encoded —
and then the very last step, creating the artist folder, raised EACCES. The
bandwidth was already spent and the log said only "Permission denied".
"""

import errno
import os
import tempfile
import threading

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


def test_concurrent_checks_do_not_invent_a_permissions_problem(music_dir):
    """Seen in the wild, and it cost an evening of chowning a healthy mount.

    Two download workers requeued at the same instant both probed /music, both
    wrote the one fixed probe name, and the second to unlink it got ENOENT.
    That surfaced as "/music is not writable (No such file or directory).
    Running as 0:0, directory is owned by 0:0" — a message that names the same
    uid twice and is therefore self-evidently not about permissions.
    """
    failures: list[str] = []
    start = threading.Barrier(4)

    def hammer():
        start.wait()
        for _ in range(50):
            problem = config.music_dir_problem()
            if problem:
                failures.append(problem)

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == [], "a writable directory must never report a problem"


def test_the_probe_cleans_up_after_itself(music_dir):
    config.music_dir_problem()
    assert list(music_dir.iterdir()) == [], "the write probe must not be left behind"


def test_a_vanished_mount_is_not_blamed_on_PUID(music_dir, monkeypatch):
    """ENOENT and ESTALE mean the share dropped, not that the uid is wrong.

    Telling someone to reconcile PUID/PGID when the mount has gone sends them
    off to chown a directory that was never the problem.
    """
    def gone(*args, **kwargs):
        raise OSError(errno.ESTALE, "Stale file handle")

    monkeypatch.setattr(tempfile, "mkstemp", gone)
    problem = config.music_dir_problem()

    assert "Stale file handle" in problem
    assert "not a permissions error" in problem
    assert "PUID/PGID" not in problem, "the fix here is the mount, not the uid"


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


# ─── The data directory ────────────────────────────────────────────────────
#
# /config is the first thing to break when PUID changes, and the worst placed
# to break quietly: the database, the download scratch space and yt-dlp's cache
# all live there.


def test_a_writable_data_directory_reports_no_problem(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert config.data_dir_problem() == ""


@pytest.mark.skipif(os.getuid() == 0, reason="root can write to anything")
def test_an_unwritable_data_directory_names_the_uids(tmp_path, monkeypatch):
    """Otherwise this arrives as sqlite's "unable to open database file"."""
    data = tmp_path / "config"
    data.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)

    data.chmod(0o500)
    try:
        problem = config.data_dir_problem()
    finally:
        data.chmod(0o700)

    assert str(data) in problem, "the message must name the directory"
    assert "not writable" in problem
    assert "PUID/PGID" in problem, "changing PUID is the usual cause"


def test_each_directory_is_reported_by_its_own_name(tmp_path, monkeypatch):
    """The two probes must not blame each other's path — they did share code."""
    music, data = tmp_path / "music", tmp_path / "config"
    music.mkdir()
    monkeypatch.setattr(config, "MUSIC_DIR", music)
    monkeypatch.setattr(config, "DATA_DIR", data)  # deliberately absent

    assert config.music_dir_problem() == ""
    problem = config.data_dir_problem()
    assert str(data) in problem
    assert str(music) not in problem
