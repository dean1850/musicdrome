"""Tests for M3U parsing, entry resolution and the import lifecycle."""

from __future__ import annotations

from pathlib import Path

import pytest

from musicdrome.services.playlistfile import (
    TrackIndex,
    export_m3u,
    import_all,
    parse_m3u_bytes,
    parse_m3u_text,
    resolve_entry,
)

LIBRARY = "/music"


# ─── Parsing ───────────────────────────────────────────────────────────────


def test_parses_extended_directives():
    document = parse_m3u_text(
        "#EXTM3U\n"
        "#PLAYLIST:Rainy Day\n"
        "#EXTINF:245,Aurora Fields - First Light\n"
        "Aurora Fields/First Light.mp3\n"
        "\n"
        "# a bare comment\n"
        "#EXTINF:180,Just A Title\n"
        "second.mp3\n"
    )

    assert document.name == "Rainy Day"
    assert len(document.entries) == 2

    first = document.entries[0]
    assert first.target == "Aurora Fields/First Light.mp3"
    assert (first.artist, first.title, first.duration) == ("Aurora Fields", "First Light", 245)

    second = document.entries[1]
    assert (second.artist, second.title) == ("", "Just A Title")


def test_parses_a_plain_playlist_without_a_header():
    document = parse_m3u_text("one.mp3\ntwo.mp3\n", name="Simple")
    assert document.name == "Simple"
    assert [entry.target for entry in document.entries] == ["one.mp3", "two.mp3"]


def test_handles_crlf_and_a_byte_order_mark():
    document = parse_m3u_bytes(
        "﻿#EXTM3U\r\n#EXTINF:100,A - B\r\ntrack.mp3\r\n".encode("utf-8"),
        name="Windows",
    )
    assert [entry.target for entry in document.entries] == ["track.mp3"]
    assert document.entries[0].artist == "A"


def test_decodes_a_windows_codepage():
    document = parse_m3u_bytes("Bj\xf6rk/Jo\xf0.mp3\n".encode("cp1252"), name="Latin")
    assert document.entries[0].target == "Björk/Joð.mp3"


def test_ignores_extinf_attributes_before_the_comma():
    document = parse_m3u_text('#EXTINF:-1 tvg-id="x",Some Title\nfile.mp3\n')
    assert document.entries[0].duration == 0
    assert document.entries[0].title == "Some Title"


def test_metadata_does_not_leak_to_the_next_entry():
    document = parse_m3u_text("#EXTINF:10,A - B\nfirst.mp3\nsecond.mp3\n")
    assert document.entries[1].artist == ""
    assert document.entries[1].title == ""


# ─── Resolution ────────────────────────────────────────────────────────────


@pytest.fixture
def index() -> TrackIndex:
    built = TrackIndex()
    built.add(1, f"{LIBRARY}/Aurora Fields/Northern Lights/01 - First Light.mp3",
              "First Light", "Aurora Fields")
    built.add(2, f"{LIBRARY}/Aurora Fields/Northern Lights/02 - Glacier Song.mp3",
              "Glacier Song", "Aurora Fields")
    built.add(3, f"{LIBRARY}/The Ledger Lines/Paper Trails/01 - Receipts.flac",
              "Receipts", "The Ledger Lines")
    return built


def resolve(target: str, index: TrackIndex, *, base: str = f"{LIBRARY}/Playlists", **meta):
    document = parse_m3u_text(
        (f"#EXTINF:0,{meta['artist']} - {meta['title']}\n" if meta else "") + target + "\n"
    )
    return resolve_entry(document.entries[0], Path(base), index)


def test_resolves_a_path_relative_to_the_playlist_file(index):
    """Downtify's own shape: the m3u in Playlists/, tracks one level up."""
    assert resolve("../Aurora Fields/Northern Lights/01 - First Light.mp3", index) == 1


def test_resolves_an_absolute_path(index):
    assert resolve(f"{LIBRARY}/The Ledger Lines/Paper Trails/01 - Receipts.flac", index) == 3


def test_resolves_case_insensitively(index):
    assert resolve("../AURORA FIELDS/northern lights/02 - GLACIER SONG.mp3", index) == 2


def test_resolves_a_windows_path(index):
    assert resolve("..\\Aurora Fields\\Northern Lights\\01 - First Light.mp3", index) == 1


def test_resolves_a_percent_encoded_file_uri(index):
    assert resolve(
        "file:///music/Aurora%20Fields/Northern%20Lights/02%20-%20Glacier%20Song.mp3", index
    ) == 2


def test_resolves_a_library_mounted_at_another_root(index):
    """The tail of the path is the same even when the mount point is not."""
    assert resolve(
        "/some/other/box/Aurora Fields/Northern Lights/01 - First Light.mp3", index
    ) == 1


def test_resolves_by_metadata_when_the_path_is_hopeless(index):
    assert resolve(
        "/nowhere/at/all/xyz.mp3", index, artist="The Ledger Lines", title="Receipts"
    ) == 3


def test_ignores_a_remote_url(index):
    assert resolve("https://example.com/stream.mp3", index) is None
    assert resolve("http://example.com/a.mp3", index, artist="A", title="Receipts") is None


def test_returns_none_for_a_track_that_is_not_in_the_library(index):
    assert resolve("../Someone Else/Unknown/01 - Nope.mp3", index) is None


def test_refuses_to_guess_between_two_equally_good_matches():
    """Two albums, same file name — a tail match must not pick one at random."""
    ambiguous = TrackIndex()
    ambiguous.add(1, f"{LIBRARY}/Artist/Album A/01 - Intro.mp3", "Intro", "Artist")
    ambiguous.add(2, f"{LIBRARY}/Artist/Album B/01 - Intro.mp3", "Intro", "Artist")

    assert resolve("/elsewhere/01 - Intro.mp3", ambiguous) is None
    # ...but a deeper tail is unambiguous again
    assert resolve("/elsewhere/Album B/01 - Intro.mp3", ambiguous) == 2


def test_matches_across_a_differing_extension():
    """A downloader may write .mp3 for a track the library holds as .flac."""
    index = TrackIndex()
    index.add(7, f"{LIBRARY}/Artist/Album/03 - Song.flac", "Song", "Artist")
    assert resolve("/elsewhere/Artist/Album/03 - Song.mp3", index) == 7


# ─── Import lifecycle ──────────────────────────────────────────────────────


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A tiny library on disk with rows in the database to match."""
    from musicdrome.auth import create_user
    from musicdrome.config import settings
    from musicdrome.db import init_db, session_scope
    from musicdrome.models import Album, Artist, Playlist, PlaylistTrack, Track, User

    monkeypatch.setattr(settings, "music_dir", tmp_path)
    monkeypatch.setattr(settings, "playlist_import_dirs", "")

    init_db()
    with session_scope() as db:
        for model in (PlaylistTrack, Playlist, Track, Album, Artist, User):
            db.query(model).delete()
        db.commit()

    with session_scope() as db:
        create_user(db, "admin", "password123", is_admin=True)
        for number, title in ((1, "First Light"), (2, "Glacier Song")):
            path = tmp_path / "Aurora Fields" / f"{number:02d} - {title}.mp3"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"")
            db.add(
                Track(
                    path=str(path),
                    title=title,
                    artist_name="Aurora Fields",
                    duration=200,
                    track_number=number,
                )
            )
        db.commit()

    return tmp_path


def write_playlist(root: Path, name: str, body: str) -> Path:
    folder = root / "Playlists"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(body, encoding="utf-8")
    return path


def only_playlist():
    from sqlalchemy import select

    from musicdrome.db import session_scope
    from musicdrome.models import Playlist

    with session_scope() as db:
        playlist = db.scalar(select(Playlist))
        if playlist is None:
            return None
        return {
            "name": playlist.name,
            "songs": playlist.song_count,
            "missing": playlist.import_missing,
            "sync": playlist.sync,
            "public": playlist.public,
            "titles": [entry.track.title for entry in playlist.entries],
        }


def test_imports_updates_and_prunes(library):
    path = write_playlist(
        library,
        "Rainy Day.m3u",
        "#EXTM3U\n"
        "#PLAYLIST:Rainy Day Mix\n"
        "../Aurora Fields/01 - First Light.mp3\n"
        "../Aurora Fields/99 - Not Downloaded Yet.mp3\n",
    )

    stats = import_all()
    assert (stats["created"], stats["missing"]) == (1, 1)

    playlist = only_playlist()
    assert playlist["name"] == "Rainy Day Mix"
    assert playlist["titles"] == ["First Light"]
    assert playlist["missing"] == 1
    assert playlist["sync"] is True

    # Re-running changes nothing
    assert import_all()["updated"] == 0

    # Editing the file re-syncs the track list
    path.write_text(
        "#EXTM3U\n../Aurora Fields/02 - Glacier Song.mp3\n", encoding="utf-8"
    )
    assert import_all()["updated"] == 1
    assert only_playlist()["titles"] == ["Glacier Song"]
    assert only_playlist()["name"] == "Rainy Day Mix"  # the rename is not undone

    # Deleting the file deletes the playlist
    path.unlink()
    assert import_all()["deleted"] == 1
    assert only_playlist() is None


def test_a_hand_edited_playlist_stops_following_its_file(library):
    from sqlalchemy import select

    from musicdrome.db import session_scope
    from musicdrome.models import Playlist
    from musicdrome.services.playlists import detach

    path = write_playlist(
        library, "Mine.m3u", "#EXTM3U\n../Aurora Fields/01 - First Light.mp3\n"
    )
    import_all()

    with session_scope() as db:
        playlist = db.scalar(select(Playlist))
        detach(playlist)
        db.commit()

    path.write_text("#EXTM3U\n../Aurora Fields/02 - Glacier Song.mp3\n", encoding="utf-8")
    assert import_all()["updated"] == 0
    assert only_playlist()["titles"] == ["First Light"]

    # ...and its file disappearing no longer takes the playlist with it
    path.unlink()
    assert import_all()["deleted"] == 0
    assert only_playlist() is not None


def test_an_entry_resolves_once_its_track_is_added(library):
    """The missing-entry count is what makes a playlist eligible for a retry."""
    from musicdrome.db import session_scope
    from musicdrome.models import Track

    write_playlist(
        library,
        "Later.m3u",
        "#EXTM3U\n"
        "../Aurora Fields/01 - First Light.mp3\n"
        "../Aurora Fields/03 - Polar Drift.mp3\n",
    )
    import_all()
    assert only_playlist()["missing"] == 1

    latecomer = library / "Aurora Fields" / "03 - Polar Drift.mp3"
    latecomer.write_bytes(b"")
    with session_scope() as db:
        db.add(
            Track(
                path=str(latecomer),
                title="Polar Drift",
                artist_name="Aurora Fields",
                duration=210,
                track_number=3,
            )
        )
        db.commit()

    assert import_all()["updated"] == 1
    assert only_playlist()["titles"] == ["First Light", "Polar Drift"]
    assert only_playlist()["missing"] == 0


def test_export_writes_library_relative_paths(library):
    from sqlalchemy import select

    from musicdrome.db import session_scope
    from musicdrome.models import Playlist

    write_playlist(
        library, "Export me.m3u", "#EXTM3U\n../Aurora Fields/01 - First Light.mp3\n"
    )
    import_all()

    with session_scope() as db:
        rendered = export_m3u(db, db.scalar(select(Playlist)))

    assert rendered.splitlines() == [
        "#EXTM3U",
        "#PLAYLIST:Export me",
        "#EXTINF:200,Aurora Fields - First Light",
        "Aurora Fields/01 - First Light.mp3",
    ]
