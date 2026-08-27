"""Deleting downloads in bulk, and clearing up after them.

The per-row delete has always been able to unlink the file it made. What it
never did was tidy up around it: the entry in Musicdrome.m3u stayed, pointing
at nothing, and the Artist/Album folders it emptied stayed too. One stale entry
is a nuisance. A hundred and seventy-eight of them is a playlist that imports
clean into a music server and fails on every track.

These tests fix the three things that have to hold when a selection is deleted:
the files go, the playlist stops naming them, and a download a worker is still
holding is left strictly alone.
"""

from __future__ import annotations

import shutil

import pytest

from app import config, db, download


@pytest.fixture(autouse=True)
def clean_library():
    """The music directory outlives a test; what a test puts in it must not."""

    def sweep():
        if not config.MUSIC_DIR.is_dir():
            return
        for child in config.MUSIC_DIR.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()

    config.MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    sweep()
    yield
    sweep()


@pytest.fixture
def downloaded():
    """A finished download with a real file on disk. Returns its row id."""

    def add(artist: str, title: str, album: str = "Album",
            status: str = "done", suggestion_id: int | None = None,
            path: str | None = None) -> int:
        target = (
            config.MUSIC_DIR / artist / album / f"{title}.opus"
            if path is None
            else config.MUSIC_DIR / path
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"not really an opus file")

        with db.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO downloads (suggestion_id, track_key, artist, title, album, "
                "path, status, bytes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (suggestion_id, f"{artist}|{title}".lower(), artist, title, album,
                 str(target), status, 23, db.now()),
            )
            return cursor.lastrowid

    return add


@pytest.fixture
def suggestion_row():
    """A suggestion in the 'downloaded' state. Returns its id."""

    def add(artist: str, title: str) -> int:
        with db.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO suggestions (artist, title, track_key, match, status, "
                "created_at) VALUES (?, ?, ?, 90, 'downloaded', ?)",
                (artist, title, f"{artist}|{title}".lower(), db.now()),
            )
            return cursor.lastrowid

    return add


def rows() -> list[dict]:
    with db.connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM downloads ORDER BY id")]


# ─── The batch itself ──────────────────────────────────────────────────────


def test_a_batch_removes_every_row_and_every_file(downloaded):
    first = downloaded("Boards of Canada", "Roygbiv")
    second = downloaded("Mazzy Star", "Fade Into You")

    result = download.remove_many([first, second])

    assert result["removed"] == 2
    assert result["files_deleted"] == 2
    assert rows() == []
    assert not (config.MUSIC_DIR / "Boards of Canada").exists()


def test_an_in_flight_download_is_left_alone(downloaded):
    """The worker holds it. Deleting the row does not stop the download — it
    only guarantees the file it writes will belong to nothing."""
    done = downloaded("Talk Talk", "New Grass")
    running = downloaded("Burial", "Archangel", status="downloading")
    queued = downloaded("Portishead", "Glory Box", status="queued")

    result = download.remove_many([done, running, queued])

    # Two folders: the album, and the artist above it once the album is gone.
    assert result == {"removed": 1, "files_deleted": 1, "skipped": 2,
                      "playlist_pruned": 0, "folders_removed": 2}
    assert [row["id"] for row in rows()] == [running, queued]
    # And their files are still there, because the workers are still using them.
    assert (config.MUSIC_DIR / "Burial" / "Album" / "Archangel.opus").exists()


def test_a_batch_of_nothing_but_in_flight_rows_changes_nothing(downloaded):
    queued = downloaded("Burial", "Archangel", status="queued")

    assert download.remove_many([queued])["skipped"] == 1
    assert [row["id"] for row in rows()] == [queued]


def test_the_tracks_become_suggestable_again(downloaded, suggestion_row):
    suggestion = suggestion_row("Boards of Canada", "Roygbiv")
    download_id = downloaded("Boards of Canada", "Roygbiv", suggestion_id=suggestion)

    download.remove_many([download_id])

    with db.connect() as conn:
        row = conn.execute(
            "SELECT status FROM suggestions WHERE id = ?", (suggestion,)
        ).fetchone()
    assert row["status"] == "new"


def test_duplicate_and_unknown_ids_are_harmless(downloaded):
    download_id = downloaded("Talk Talk", "New Grass")

    result = download.remove_many([download_id, download_id, 9999])

    assert result["removed"] == 1
    assert rows() == []


def test_an_empty_batch_does_nothing():
    assert download.remove_many([])["removed"] == 0


def test_the_rows_can_go_without_the_files(downloaded):
    download_id = downloaded("Mazzy Star", "Fade Into You")

    result = download.remove_many([download_id], delete_file=False)

    assert result["removed"] == 1
    assert result["files_deleted"] == 0
    assert (config.MUSIC_DIR / "Mazzy Star" / "Album" / "Fade Into You.opus").exists()


def test_a_path_outside_the_library_is_refused(downloaded, tmp_path):
    """A corrupt row, or a library that moved. Either way the row goes and the
    file does not: this is the one place Musicdrome deletes what it is told to,
    and it will not be told to delete /etc."""
    outsider = tmp_path / "somebody-elses.opus"
    outsider.write_bytes(b"leave me alone")

    with db.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO downloads (track_key, artist, title, path, status, created_at) "
            "VALUES ('x', 'A', 'B', ?, 'done', ?)",
            (str(outsider), db.now()),
        )
        download_id = cursor.lastrowid

    result = download.remove_many([download_id])

    assert result["removed"] == 1
    assert result["files_deleted"] == 0
    assert outsider.exists()


# ─── The playlist ──────────────────────────────────────────────────────────


def playlist_lines() -> list[str]:
    return [
        line for line in
        config.PLAYLIST_PATH.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]


def test_a_deleted_track_stops_being_listed_in_the_playlist(downloaded):
    kept = config.MUSIC_DIR / "Talk Talk" / "Album" / "New Grass.opus"
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_bytes(b"still here")
    download.append_to_playlist(kept, {"artist": "Talk Talk", "title": "New Grass"})

    doomed = config.MUSIC_DIR / "Burial" / "Album" / "Archangel.opus"
    download_id = downloaded("Burial", "Archangel")
    download.append_to_playlist(doomed, {"artist": "Burial", "title": "Archangel"})

    assert len(playlist_lines()) == 2

    result = download.remove_many([download_id])

    assert result["playlist_pruned"] == 1
    assert len(playlist_lines()) == 1
    assert "Archangel" not in config.PLAYLIST_PATH.read_text(encoding="utf-8")
    # The one that stayed keeps its #EXTINF, not just its path.
    assert "#EXTINF" in config.PLAYLIST_PATH.read_text(encoding="utf-8")


def test_an_absolute_playlist_entry_is_matched_too(downloaded):
    """Entries are written relative to the playlist, but a library that has
    moved leaves absolute ones behind. Both name the same file."""
    target = config.MUSIC_DIR / "Burial" / "Album" / "Archangel.opus"
    download_id = downloaded("Burial", "Archangel")

    config.PLAYLIST_DIR.mkdir(parents=True, exist_ok=True)
    config.PLAYLIST_PATH.write_text(f"#EXTM3U\n{target}\n", encoding="utf-8")

    assert download.remove_many([download_id])["playlist_pruned"] == 1
    assert playlist_lines() == []


def test_a_playlist_that_names_nothing_deleted_is_left_exactly_as_it_was(downloaded):
    kept = config.MUSIC_DIR / "Talk Talk" / "Album" / "New Grass.opus"
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_bytes(b"still here")
    download.append_to_playlist(kept, {"artist": "Talk Talk", "title": "New Grass"})
    before = config.PLAYLIST_PATH.read_text(encoding="utf-8")

    download.remove_many([downloaded("Burial", "Archangel")])

    assert config.PLAYLIST_PATH.read_text(encoding="utf-8") == before


def test_no_playlist_is_not_a_failure(downloaded):
    assert not config.PLAYLIST_PATH.exists()
    assert download.remove_many([downloaded("Burial", "Archangel")])["playlist_pruned"] == 0


# ─── The folders left behind ───────────────────────────────────────────────


def test_emptied_folders_are_removed_all_the_way_up(downloaded):
    download_id = downloaded("Boards of Canada", "Roygbiv", album="Music Has the Right")

    download.remove_many([download_id])

    assert not (config.MUSIC_DIR / "Boards of Canada" / "Music Has the Right").exists()
    assert not (config.MUSIC_DIR / "Boards of Canada").exists()


def test_a_folder_that_still_holds_something_stays(downloaded):
    first = downloaded("Boards of Canada", "Roygbiv", album="Music Has the Right")
    downloaded("Boards of Canada", "Telephasic Workshop", album="Music Has the Right")

    download.remove_many([first])

    album = config.MUSIC_DIR / "Boards of Canada" / "Music Has the Right"
    assert album.is_dir()
    assert (album / "Telephasic Workshop.opus").exists()


def test_the_library_root_is_never_removed(downloaded):
    """MUSIC_DIR is usually a mount point. Emptying the library is not a
    reason to unmount it."""
    download_id = downloaded("Solo", "Track", path="Track.opus")

    download.remove_many([download_id])

    assert config.MUSIC_DIR.is_dir()


# ─── The single-row delete does the same tidying up ────────────────────────


def test_removing_one_download_prunes_the_playlist_too(downloaded):
    target = config.MUSIC_DIR / "Burial" / "Album" / "Archangel.opus"
    download_id = downloaded("Burial", "Archangel")
    download.append_to_playlist(target, {"artist": "Burial", "title": "Archangel"})

    assert download.remove(download_id, delete_file=True) is True

    assert playlist_lines() == []
    assert not (config.MUSIC_DIR / "Burial").exists()


def test_removing_one_download_without_its_file_leaves_the_playlist_alone(downloaded):
    target = config.MUSIC_DIR / "Burial" / "Album" / "Archangel.opus"
    download_id = downloaded("Burial", "Archangel")
    download.append_to_playlist(target, {"artist": "Burial", "title": "Archangel"})

    download.remove(download_id, delete_file=False)

    assert playlist_lines() == [download.playlist_entry(target)]


def test_removing_a_download_that_is_not_there_is_false():
    assert download.remove(9999, delete_file=True) is False
