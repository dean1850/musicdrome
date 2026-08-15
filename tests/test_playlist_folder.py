"""Where the playlist goes, and what happens to the one already written.

The paths inside an m3u are relative to the folder holding it, so the folder is
not a cosmetic setting — change it without recomputing the entries and every
line points somewhere else. A music server imports that as an empty playlist
rather than reporting it as broken, which is the failure mode these tests
exist to make impossible: it is indistinguishable, from the outside, from the
playlist never having been imported at all.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app import config, download


@pytest.fixture
def playlists_at():
    """Point the playlist at a folder for the duration of one test."""
    original_dir, original_path = config.PLAYLIST_DIR, config.PLAYLIST_PATH

    def move(folder: str | Path) -> Path:
        directory = (
            config._playlist_dir(folder) if isinstance(folder, str) else Path(folder)
        )
        config.PLAYLIST_DIR = directory
        config.PLAYLIST_PATH = directory / f"{config.PLAYLIST_NAME}.m3u"
        return directory

    yield move
    config.PLAYLIST_DIR, config.PLAYLIST_PATH = original_dir, original_path


@pytest.fixture(autouse=True)
def clean_folders():
    """The music directory outlives a test; the playlist folders must not."""
    import shutil

    def sweep():
        for name in ("_playlists", "playlist", "media", "deep", "Playlists"):
            shutil.rmtree(config.MUSIC_DIR / name, ignore_errors=True)
        for stray in config.MUSIC_DIR.glob("*.m3u"):
            stray.unlink()

    sweep()
    yield
    sweep()


# ─── Reading the setting ───────────────────────────────────────────────────


def test_a_bare_name_lands_under_the_music_folder():
    assert config._playlist_dir("playlist") == config.MUSIC_DIR / "playlist"


def test_a_nested_name_is_followed():
    assert config._playlist_dir("media/playlists") == config.MUSIC_DIR / "media" / "playlists"


@pytest.mark.parametrize("value", ["", ".", "   ", '""', "'.'"])
def test_empty_or_dot_means_the_library_root(value):
    """The most importable spot: no ../ in any entry, and it matches almost
    any PlaylistsPath a music server has been given."""
    assert config._playlist_dir(value) == config.MUSIC_DIR


def test_an_absolute_path_is_used_exactly_as_given():
    assert config._playlist_dir("/srv/playlists") == Path("/srv/playlists")


def test_surrounding_whitespace_and_quotes_are_ignored():
    """`.env` files routinely carry both, and neither is part of the name."""
    assert config._playlist_dir('  "playlist"  ') == config.MUSIC_DIR / "playlist"


def test_an_unset_variable_is_distinguishable_from_an_empty_one(monkeypatch):
    """Empty is a real answer here, so it must not collapse into the default."""
    monkeypatch.delenv("PLAYLIST_FOLDER", raising=False)
    monkeypatch.delenv("MUSICDROME_PLAYLIST_FOLDER", raising=False)
    assert config._env_present("MUSICDROME_PLAYLIST_FOLDER", "PLAYLIST_FOLDER") is None

    monkeypatch.setenv("PLAYLIST_FOLDER", "")
    assert config._env_present("MUSICDROME_PLAYLIST_FOLDER", "PLAYLIST_FOLDER") == ""


def test_the_container_spelling_wins_over_the_bare_one(monkeypatch):
    monkeypatch.setenv("MUSICDROME_PLAYLIST_FOLDER", "inside")
    monkeypatch.setenv("PLAYLIST_FOLDER", "outside")
    assert config._env_present("MUSICDROME_PLAYLIST_FOLDER", "PLAYLIST_FOLDER") == "inside"


# ─── Writing the entries ───────────────────────────────────────────────────


def track(*parts: str) -> Path:
    return config.MUSIC_DIR.joinpath(*parts)


def test_a_playlist_beside_the_music_needs_no_dot_dot(playlists_at):
    """At the library root the entries are plain relative paths."""
    playlists_at(".")
    assert download.playlist_entry(track("Radiohead", "OK Computer", "06 - Karma Police.opus")) == (
        "Radiohead/OK Computer/06 - Karma Police.opus"
    )


def test_a_playlist_one_level_down_climbs_once(playlists_at):
    playlists_at("playlist")
    assert download.playlist_entry(track("Radiohead", "OK Computer", "06 - Karma Police.opus")) == (
        "../Radiohead/OK Computer/06 - Karma Police.opus"
    )


def test_a_playlist_two_levels_down_climbs_twice(playlists_at):
    """The old code prepended a literal '..' and was wrong for exactly this."""
    playlists_at("media/playlists")
    assert download.playlist_entry(track("Radiohead", "OK Computer", "06 - Karma Police.opus")) == (
        "../../Radiohead/OK Computer/06 - Karma Police.opus"
    )


def test_a_playlist_outside_the_library_uses_absolute_paths(playlists_at):
    """A relative path between two unrelated trees breaks when either moves."""
    playlists_at(Path("/srv/playlists"))
    entry = download.playlist_entry(track("Radiohead", "OK Computer", "06 - Karma Police.opus"))
    assert Path(entry).is_absolute()


def test_a_track_outside_the_library_uses_an_absolute_path(playlists_at):
    playlists_at("playlist")
    assert download.playlist_entry(Path("/elsewhere/A/B.opus")) == "/elsewhere/A/B.opus"


def test_appending_writes_the_entry_for_the_configured_folder(playlists_at):
    playlists_at(".")
    path = track("Radiohead", "OK Computer", "06 - Karma Police.opus")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")

    playlist = Path(download.append_to_playlist(
        path, {"artist": "Radiohead", "title": "Karma Police", "duration": 264}))

    assert playlist == config.MUSIC_DIR / f"{config.PLAYLIST_NAME}.m3u"
    lines = playlist.read_text().splitlines()
    assert lines[2] == "Radiohead/OK Computer/06 - Karma Police.opus"


# ─── Rewriting an entry for a moved playlist ───────────────────────────────


def test_an_entry_moving_between_folders_of_equal_depth_is_unchanged():
    old, new = config.MUSIC_DIR / "_playlists", config.MUSIC_DIR / "playlist"
    assert download.rewrite_entry("../A/B/x.opus", old, new) == "../A/B/x.opus"


def test_an_entry_moving_up_to_the_root_loses_its_dot_dot():
    old, new = config.MUSIC_DIR / "_playlists", config.MUSIC_DIR
    assert download.rewrite_entry("../A/B/x.opus", old, new) == "A/B/x.opus"


def test_an_entry_moving_deeper_gains_a_dot_dot():
    old, new = config.MUSIC_DIR / "_playlists", config.MUSIC_DIR / "media" / "playlists"
    assert download.rewrite_entry("../A/B/x.opus", old, new) == "../../A/B/x.opus"


def test_an_absolute_entry_is_left_alone():
    """It never depended on where the playlist was."""
    old, new = config.MUSIC_DIR / "_playlists", config.MUSIC_DIR
    assert download.rewrite_entry("/music/A/B/x.opus", old, new) == "/music/A/B/x.opus"


def test_a_url_entry_is_left_alone():
    old, new = config.MUSIC_DIR / "_playlists", config.MUSIC_DIR
    assert download.rewrite_entry("http://host/stream.mp3", old, new) == "http://host/stream.mp3"


def test_comment_lines_are_left_alone():
    old, new = config.MUSIC_DIR / "_playlists", config.MUSIC_DIR
    assert download.rewrite_entry("#EXTINF:264,A - B", old, new) == "#EXTINF:264,A - B"


def test_a_rewritten_entry_still_points_at_the_same_file():
    """The property that actually matters, checked by resolving both ends."""
    old, new = config.MUSIC_DIR / "_playlists", config.MUSIC_DIR / "media" / "playlists"
    before = Path(os.path.normpath(old / "../A/B/x.opus"))
    after = Path(os.path.normpath(new / download.rewrite_entry("../A/B/x.opus", old, new)))
    assert before == after


# ─── Migrating the folder ──────────────────────────────────────────────────


def seed_legacy(*entries: str) -> Path:
    """A playlist sitting in the old hardcoded `_playlists` folder."""
    legacy = config.LEGACY_PLAYLIST_DIR
    legacy.mkdir(parents=True, exist_ok=True)
    playlist = legacy / f"{config.PLAYLIST_NAME}.m3u"
    body = "#EXTM3U\n"
    for entry in entries:
        body += f"#EXTINF:264,A - B\n{entry}\n"
    playlist.write_text(body)
    return playlist


def entries_of(playlist: Path) -> list[str]:
    return [
        line for line in playlist.read_text().splitlines()
        if line and not line.startswith("#")
    ]


def test_the_old_playlist_is_carried_across(playlists_at):
    seed_legacy("../Radiohead/OK Computer/06 - Karma Police.opus")
    new_dir = playlists_at("playlist")

    assert download.migrate_playlist_folder() == 1

    moved = new_dir / f"{config.PLAYLIST_NAME}.m3u"
    assert moved.exists()
    assert not (config.LEGACY_PLAYLIST_DIR / f"{config.PLAYLIST_NAME}.m3u").exists()
    assert entries_of(moved) == ["../Radiohead/OK Computer/06 - Karma Police.opus"]


def test_the_entries_are_rewritten_for_the_new_depth(playlists_at):
    """Moving the file without this leaves a playlist of paths to nothing."""
    seed_legacy("../Radiohead/OK Computer/06 - Karma Police.opus")
    new_dir = playlists_at(".")

    download.migrate_playlist_folder()

    moved = new_dir / f"{config.PLAYLIST_NAME}.m3u"
    assert entries_of(moved) == ["Radiohead/OK Computer/06 - Karma Police.opus"]


def test_the_extinf_lines_survive_the_move(playlists_at):
    seed_legacy("../A/B/x.opus")
    new_dir = playlists_at("playlist")

    download.migrate_playlist_folder()

    text = (new_dir / f"{config.PLAYLIST_NAME}.m3u").read_text()
    assert text.count("#EXTM3U") == 1
    assert "#EXTINF:264,A - B" in text


def test_nothing_happens_when_the_folder_has_not_changed(playlists_at):
    seed_legacy("../A/B/x.opus")
    playlists_at(config.LEGACY_PLAYLIST_DIR)

    assert download.migrate_playlist_folder() == 0
    assert (config.LEGACY_PLAYLIST_DIR / f"{config.PLAYLIST_NAME}.m3u").exists()


def test_nothing_happens_when_there_is_no_old_folder(playlists_at):
    playlists_at("playlist")
    assert download.migrate_playlist_folder() == 0


def test_an_unreadable_old_folder_does_not_stop_the_boot(playlists_at):
    """This runs inside the boot lifespan; raising here means the app never starts.

    The refusal is injected rather than produced with ``chmod``, because the
    container these tests usually run in is root and root reads a 000 directory
    quite happily — the test would then pass by never reaching the code it is
    aimed at.
    """
    seed_legacy("../A/B/x.opus")
    playlists_at("playlist")

    def refuse(self, pattern):
        raise PermissionError(13, "Permission denied")

    original = Path.glob
    Path.glob = refuse
    try:
        assert download.migrate_playlist_folder() == 0
    finally:
        Path.glob = original

    # And the playlist is still where it was, not half-moved.
    assert (config.LEGACY_PLAYLIST_DIR / f"{config.PLAYLIST_NAME}.m3u").is_file()


def test_migrating_twice_is_harmless(playlists_at):
    seed_legacy("../A/B/x.opus")
    new_dir = playlists_at("playlist")

    assert download.migrate_playlist_folder() == 1
    assert download.migrate_playlist_folder() == 0
    assert entries_of(new_dir / f"{config.PLAYLIST_NAME}.m3u") == ["../A/B/x.opus"]


def test_two_real_playlists_are_merged_rather_than_one_overwritten(playlists_at):
    """Both are download history; picking one to delete is not ours to do."""
    seed_legacy("../A/Old/x.opus")
    new_dir = playlists_at("playlist")
    new_dir.mkdir(parents=True, exist_ok=True)
    (new_dir / f"{config.PLAYLIST_NAME}.m3u").write_text(
        "#EXTM3U\n#EXTINF:1,C - D\n../B/New/y.opus\n"
    )

    download.migrate_playlist_folder()

    assert entries_of(new_dir / f"{config.PLAYLIST_NAME}.m3u") == [
        "../B/New/y.opus", "../A/Old/x.opus",
    ]


def test_a_track_listed_in_both_is_not_doubled(playlists_at):
    seed_legacy("../A/B/x.opus")
    new_dir = playlists_at("playlist")
    new_dir.mkdir(parents=True, exist_ok=True)
    (new_dir / f"{config.PLAYLIST_NAME}.m3u").write_text("#EXTM3U\n../A/B/x.opus\n")

    download.migrate_playlist_folder()

    assert entries_of(new_dir / f"{config.PLAYLIST_NAME}.m3u") == ["../A/B/x.opus"]


def test_the_legacy_per_scan_files_come_across_too(playlists_at):
    """So the consolidation that runs next can still find them."""
    config.LEGACY_PLAYLIST_DIR.mkdir(parents=True, exist_ok=True)
    (config.LEGACY_PLAYLIST_DIR / "musicdrome-scan-0001.m3u").write_text(
        "#EXTM3U\n../A/B/x.opus\n"
    )
    new_dir = playlists_at("playlist")

    assert download.migrate_playlist_folder() == 1
    assert (new_dir / "musicdrome-scan-0001.m3u").exists()


def test_a_playlist_a_person_made_is_never_moved(playlists_at):
    seed_legacy("../A/B/x.opus")
    mine = config.LEGACY_PLAYLIST_DIR / "Sunday morning.m3u"
    mine.write_text("#EXTM3U\n../Nick Drake/Pink Moon/01 - Pink Moon.mp3\n")
    playlists_at("playlist")

    download.migrate_playlist_folder()

    assert mine.exists()
    assert "Pink Moon" not in (config.PLAYLIST_DIR / f"{config.PLAYLIST_NAME}.m3u").read_text()


def test_the_old_folder_is_left_when_it_still_holds_someone_elses_work(playlists_at):
    seed_legacy("../A/B/x.opus")
    (config.LEGACY_PLAYLIST_DIR / "Sunday morning.m3u").write_text("#EXTM3U\n")
    playlists_at("playlist")

    download.migrate_playlist_folder()

    assert config.LEGACY_PLAYLIST_DIR.is_dir()


def test_the_old_folder_is_tidied_away_once_it_is_empty(playlists_at):
    seed_legacy("../A/B/x.opus")
    playlists_at("playlist")

    download.migrate_playlist_folder()

    assert not config.LEGACY_PLAYLIST_DIR.exists()


def test_a_playlist_migrated_to_the_root_survives_consolidation(playlists_at):
    """The two boot steps run in this order and must not fight."""
    config.LEGACY_PLAYLIST_DIR.mkdir(parents=True, exist_ok=True)
    (config.LEGACY_PLAYLIST_DIR / f"{config.PLAYLIST_NAME}.m3u").write_text(
        "#EXTM3U\n../A/B/x.opus\n"
    )
    (config.LEGACY_PLAYLIST_DIR / "musicdrome-scan-0001.m3u").write_text(
        "#EXTM3U\n../C/D/y.opus\n"
    )
    new_dir = playlists_at(".")

    download.migrate_playlist_folder()
    download.consolidate_scan_playlists()

    playlist = new_dir / f"{config.PLAYLIST_NAME}.m3u"
    assert sorted(entries_of(playlist)) == ["A/B/x.opus", "C/D/y.opus"]
    assert not list(new_dir.glob("musicdrome-scan-*.m3u"))
