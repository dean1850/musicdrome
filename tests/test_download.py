"""Candidate scoring, filing and the .m3u — the parts that touch your library."""

from pathlib import Path

import pytest

from app import config, download
from app.download import Candidate


@pytest.fixture(autouse=True)
def empty_playlist_dir():
    """The music directory outlives a test; the playlists must not."""
    import shutil

    shutil.rmtree(config.PLAYLIST_DIR, ignore_errors=True)
    yield
    shutil.rmtree(config.PLAYLIST_DIR, ignore_errors=True)


def candidate(**overrides) -> Candidate:
    base = dict(url="https://example.test/1", title="Karma Police",
                artist="Radiohead", album="OK Computer", duration=264, source="ytmusic")
    base.update(overrides)
    return Candidate(**base)


# ─── Scoring ───────────────────────────────────────────────────────────────


def test_an_exact_match_on_all_three_signals_scores_high():
    score = download.score(candidate(), "Radiohead", "Karma Police", 264)
    assert score >= 0.9


def test_a_wrong_artist_scores_below_the_download_threshold():
    weak = candidate(artist="Some Tribute Band", title="Karma Police")
    assert download.score(weak, "Radiohead", "Karma Police", 264) < download.MIN_SCORE


def test_a_live_version_is_penalised():
    live = candidate(title="Karma Police (Live at Reading)")
    studio = candidate()
    assert download.score(live, "Radiohead", "Karma Police", 264) < download.score(
        studio, "Radiohead", "Karma Police", 264
    )


def test_a_live_track_is_not_penalised_when_a_live_track_is_what_was_asked_for():
    live = candidate(title="Karma Police (Live at Reading)")
    asked = download.score(live, "Radiohead", "Karma Police (Live at Reading)", 264)
    assert asked >= download.MIN_SCORE


def test_duration_drift_pushes_a_candidate_under_the_threshold():
    long_edit = candidate(duration=620)
    assert download.score(long_edit, "Radiohead", "Karma Police", 264) < download.score(
        candidate(), "Radiohead", "Karma Police", 264
    )


def test_without_a_reference_duration_a_clip_is_rejected():
    clip = candidate(duration=20)
    full = candidate(duration=264)
    assert download.score(clip, "Radiohead", "Karma Police", 0) < download.score(
        full, "Radiohead", "Karma Police", 0
    )


def test_remastered_titles_still_match_the_track_asked_for():
    remaster = candidate(title="Karma Police (Remastered 2016)")
    assert download.score(remaster, "Radiohead", "Karma Police", 264) >= download.MIN_SCORE


# ─── Filing ────────────────────────────────────────────────────────────────


def test_target_path_uses_the_artist_album_track_layout():
    path = download.target_path("Radiohead", "OK Computer", "Karma Police", 6)
    assert path.parent.parent.name == "Radiohead"
    assert path.parent.name == "OK Computer"
    assert path.name == "06 - Karma Police.mp3"


def test_a_missing_track_number_drops_the_prefix():
    path = download.target_path("Radiohead", "OK Computer", "Karma Police", 0)
    assert path.name == "Karma Police.mp3"


def test_a_missing_album_files_under_singles():
    path = download.target_path("Someone", "", "A Song", 0)
    assert path.parent.name == "Singles"


def test_path_separators_in_names_do_not_escape_the_layout():
    path = download.target_path("AC/DC", "Back/Slash", "Hells/Bells", 0)
    assert path.parent.parent.name == "ACDC"
    assert path.relative_to(config.MUSIC_DIR).parts == ("ACDC", "BackSlash", "HellsBells.mp3")


def test_an_existing_file_is_never_overwritten():
    first = download.target_path("Dup", "Album", "Song", 1)
    first.write_bytes(b"original")
    second = download.target_path("Dup", "Album", "Song", 1)
    assert second != first
    assert first.read_bytes() == b"original"


# ─── Playlists ─────────────────────────────────────────────────────────────


def make_track(artist: str, album: str, title: str, track_no: int = 0) -> Path:
    path = download.target_path(artist, album, title, track_no)
    path.write_bytes(b"")
    return path


def test_downloads_collect_in_one_playlist_with_relative_paths():
    first = make_track("Radiohead", "OK Computer", "Karma Police", 6)
    second = make_track("Portishead", "Dummy", "Glory Box", 3)

    playlist = Path(download.append_to_playlist(
        first, {"artist": "Radiohead", "title": "Karma Police", "duration": 264}))
    download.append_to_playlist(
        second, {"artist": "Portishead", "title": "Glory Box", "duration": 301})

    # One file for every scan, named after the app rather than a scan number.
    assert playlist == config.PLAYLIST_PATH
    lines = playlist.read_text().splitlines()
    assert lines[0] == "#EXTM3U"
    assert lines[1] == "#EXTINF:264,Radiohead - Karma Police"
    assert lines[2].startswith("../Radiohead/OK Computer/")
    assert lines[3] == "#EXTINF:301,Portishead - Glory Box"
    assert lines[4].startswith("../Portishead/Dummy/")


def test_appending_the_same_track_twice_does_not_duplicate_it():
    """A retry, or a re-download after a delete, must not double the entry."""
    path = make_track("Radiohead", "OK Computer", "Karma Police", 6)
    meta = {"artist": "Radiohead", "title": "Karma Police", "duration": 264}

    playlist = Path(download.append_to_playlist(path, meta))
    download.append_to_playlist(path, meta)

    lines = playlist.read_text().splitlines()
    assert lines.count("#EXTM3U") == 1
    assert len([line for line in lines if not line.startswith("#")]) == 1


def test_the_old_per_scan_playlists_are_merged_and_removed():
    """Installs that predate the single playlist keep their tracks."""
    config.PLAYLIST_DIR.mkdir(parents=True, exist_ok=True)
    (config.PLAYLIST_DIR / "musicdrome-scan-0001.m3u").write_text(
        "#EXTM3U\n#EXTINF:264,Radiohead - Karma Police\n../Radiohead/OK Computer/06 - Karma Police.mp3\n"
    )
    (config.PLAYLIST_DIR / "musicdrome-scan-0002.m3u").write_text(
        "#EXTM3U\n#EXTINF:301,Portishead - Glory Box\n../Portishead/Dummy/03 - Glory Box.mp3\n"
    )

    assert download.consolidate_scan_playlists() == 2

    lines = config.PLAYLIST_PATH.read_text().splitlines()
    assert lines.count("#EXTM3U") == 1
    assert "../Radiohead/OK Computer/06 - Karma Police.mp3" in lines
    assert "../Portishead/Dummy/03 - Glory Box.mp3" in lines
    assert not list(config.PLAYLIST_DIR.glob("musicdrome-scan-*.m3u"))


def test_merging_runs_once_and_skips_tracks_already_listed():
    config.PLAYLIST_DIR.mkdir(parents=True, exist_ok=True)
    config.PLAYLIST_PATH.write_text(
        "#EXTM3U\n#EXTINF:264,Radiohead - Karma Police\n../Radiohead/OK Computer/06 - Karma Police.mp3\n"
    )
    (config.PLAYLIST_DIR / "musicdrome-scan-0001.m3u").write_text(
        "#EXTM3U\n#EXTINF:264,Radiohead - Karma Police\n../Radiohead/OK Computer/06 - Karma Police.mp3\n"
    )

    assert download.consolidate_scan_playlists() == 0
    assert download.consolidate_scan_playlists() == 0  # nothing left to merge

    lines = [line for line in config.PLAYLIST_PATH.read_text().splitlines()
             if not line.startswith("#")]
    assert lines == ["../Radiohead/OK Computer/06 - Karma Police.mp3"]


def test_a_playlist_a_person_made_is_never_touched():
    config.PLAYLIST_DIR.mkdir(parents=True, exist_ok=True)
    mine = config.PLAYLIST_DIR / "Sunday morning.m3u"
    mine.write_text("#EXTM3U\n../Nick Drake/Pink Moon/01 - Pink Moon.mp3\n")
    (config.PLAYLIST_DIR / "musicdrome-scan-0001.m3u").write_text(
        "#EXTM3U\n../Radiohead/OK Computer/06 - Karma Police.mp3\n"
    )

    download.consolidate_scan_playlists()

    assert mine.exists()
    assert "Pink Moon" not in config.PLAYLIST_PATH.read_text()


# ─── Queue ─────────────────────────────────────────────────────────────────


def test_enqueue_marks_the_suggestion_and_is_idempotent(suggestion):
    from app import db

    suggestion_id = suggestion("Radiohead", "Karma Police")
    first = download.enqueue(suggestion_id)
    second = download.enqueue(suggestion_id)

    assert first == second
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
        count = conn.execute("SELECT COUNT(*) AS n FROM downloads").fetchone()["n"]
    assert row["status"] == "queued"
    assert count == 1


def test_enqueue_on_a_missing_suggestion_returns_none():
    assert download.enqueue(9999) is None


def test_auto_enqueue_is_off_by_default(suggestion):
    suggestion("A", "One", match=99)
    assert download.auto_enqueue() == 0


def test_auto_enqueue_respects_the_threshold(suggestion):
    from app import db

    db.save_settings({"auto_download": True, "auto_download_threshold": 90})
    suggestion("A", "High", match=95)
    suggestion("B", "Low", match=60)

    assert download.auto_enqueue() == 1
    with db.connect() as conn:
        titles = [row["title"] for row in conn.execute("SELECT title FROM downloads")]
    assert titles == ["High"]


def test_auto_enqueue_stops_at_the_daily_cap(suggestion):
    from app import db

    db.save_settings({"auto_download": True, "auto_download_threshold": 0, "daily_download_cap": 2})
    for index in range(5):
        suggestion(f"Artist {index}", f"Track {index}", match=80)

    assert download.auto_enqueue() == 2


def test_remove_only_deletes_files_inside_the_music_directory(tmp_path, suggestion):
    from app import db

    outsider = tmp_path / "not-ours.mp3"
    outsider.write_bytes(b"precious")

    suggestion_id = suggestion("A", "One")
    download_id = download.enqueue(suggestion_id)
    with db.connect() as conn:
        conn.execute(
            "UPDATE downloads SET status = 'done', path = ? WHERE id = ?",
            (str(outsider), download_id),
        )

    assert download.remove(download_id, delete_file=True) is True
    assert outsider.exists(), "a path outside MUSIC_DIR must never be deleted"


def test_remove_deletes_a_file_it_did_write(suggestion):
    from app import db

    path = download.target_path("Gone", "Album", "Song", 0)
    path.write_bytes(b"audio")

    suggestion_id = suggestion("Gone", "Song")
    download_id = download.enqueue(suggestion_id)
    with db.connect() as conn:
        conn.execute(
            "UPDATE downloads SET status = 'done', path = ? WHERE id = ?", (str(path), download_id)
        )

    assert download.remove(download_id, delete_file=True) is True
    assert not path.exists()


def test_best_match_returns_nothing_when_every_candidate_is_weak(monkeypatch):
    monkeypatch.setattr(download, "search_ytmusic", lambda *a, **k: [candidate(artist="Nobody", title="Different Song", duration=30)])
    monkeypatch.setattr(download, "search_youtube", lambda *a, **k: [])
    assert download.best_match("Radiohead", "Karma Police", 264) is None


def test_best_match_skips_youtube_when_youtube_music_is_confident(monkeypatch):
    calls = []
    monkeypatch.setattr(download, "search_ytmusic", lambda *a, **k: [candidate()])
    monkeypatch.setattr(download, "search_youtube", lambda *a, **k: calls.append(1) or [])

    best = download.best_match("Radiohead", "Karma Police", 264)
    assert best is not None and best.source == "ytmusic"
    assert calls == []


@pytest.mark.parametrize("title", ["Song (Karaoke Version)", "Song - 8D AUDIO", "Song [Nightcore]"])
def test_obvious_reuploads_are_rejected(title):
    junk = candidate(title=title, artist="Random Uploads", duration=200)
    assert download.score(junk, "Someone", "Song", 200) < download.MIN_SCORE
