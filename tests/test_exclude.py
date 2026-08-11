"""The exclusion index is the difference between discovery and a mirror."""

from pathlib import Path

from app import db, download, exclude
from app.norm import track_key


def test_scrobbled_tracks_are_excluded(play):
    play("Radiohead", "Karma Police")
    assert track_key("Radiohead", "Karma Police") in exclude.build()


def test_an_alias_spelling_still_matches(play):
    play("The Beatles", "Let It Be")
    assert track_key("Beatles", "Let It Be (Remastered 2009)") in exclude.build()


def test_hidden_suggestions_are_excluded(suggestion):
    suggestion("Someone", "A Song", status="hidden")
    assert track_key("Someone", "A Song") in exclude.build()


def test_a_new_suggestion_is_not_excluded(suggestion):
    suggestion("Someone", "A Song", status="new")
    assert track_key("Someone", "A Song") not in exclude.build()


def test_saved_suggestions_stay_suggestable(suggestion):
    suggestion("Someone", "A Song", status="saved")
    assert track_key("Someone", "A Song") not in exclude.build()


def test_queued_and_downloaded_suggestions_are_excluded(suggestion):
    suggestion("A", "Queued", status="queued")
    suggestion("B", "Done", status="downloaded")
    keys = exclude.build()
    assert track_key("A", "Queued") in keys
    assert track_key("B", "Done") in keys


def test_downloads_are_excluded(suggestion):
    suggestion_id = suggestion("Artist", "Track")
    download.enqueue(suggestion_id)
    assert track_key("Artist", "Track") in exclude.build()


def test_known_artists_ranks_by_play_count(play):
    for _ in range(5):
        play("Loved", "Song", at=db.now() - _ * 100)
    play("Barely", "Song")
    assert {"loved", "barely"} <= exclude.known_artists()


# ─── Library folder scan ───────────────────────────────────────────────────


# A valid MPEG-1 Layer III header: 128 kbps, 44.1 kHz, joint stereo. That makes
# every frame 417 bytes, so a run of these is a file mutagen will parse without
# needing ffmpeg to generate real audio.
SILENT_FRAME = b"\xff\xfb\x90\x64" + b"\x00" * 413
SILENT_MP3 = SILENT_FRAME * 12


def write_mp3(path: Path, artist: str, title: str) -> None:
    """A minimal tagged MP3."""
    from mutagen.easyid3 import EasyID3
    from mutagen.mp3 import MP3

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(SILENT_MP3)
    audio = MP3(path)
    audio.add_tags()
    audio.save()
    tags = EasyID3(path)
    tags["artist"] = artist
    tags["title"] = title
    tags.save()


def test_a_tagged_library_file_is_excluded(tmp_path):
    write_mp3(tmp_path / "a.mp3", "Portishead", "Glory Box")
    stats = exclude.scan_library(str(tmp_path))

    assert stats["indexed"] == 1
    assert track_key("Portishead", "Glory Box") in exclude.build()


def test_an_untagged_file_falls_back_to_its_name(tmp_path):
    path = tmp_path / "Massive Attack - Teardrop.mp3"
    path.write_bytes(SILENT_MP3)

    exclude.scan_library(str(tmp_path))
    assert track_key("Massive Attack", "Teardrop") in exclude.build()


def test_rescanning_unchanged_files_costs_nothing(tmp_path):
    write_mp3(tmp_path / "a.mp3", "Portishead", "Glory Box")
    exclude.scan_library(str(tmp_path))

    second = exclude.scan_library(str(tmp_path))
    assert second["seen"] == 1
    assert second["indexed"] == 0


def test_deleted_files_leave_the_index(tmp_path):
    path = tmp_path / "a.mp3"
    write_mp3(path, "Portishead", "Glory Box")
    exclude.scan_library(str(tmp_path))

    path.unlink()
    stats = exclude.scan_library(str(tmp_path))

    assert stats["removed"] == 1
    assert track_key("Portishead", "Glory Box") not in exclude.build()


def test_non_audio_files_are_ignored(tmp_path):
    (tmp_path / "cover.jpg").write_bytes(b"not audio")
    (tmp_path / "notes.txt").write_text("hello")
    assert exclude.scan_library(str(tmp_path))["seen"] == 0


def test_an_unset_directory_is_a_no_op():
    assert exclude.scan_library("")["seen"] == 0


def test_a_missing_directory_does_not_raise(tmp_path):
    assert exclude.scan_library(str(tmp_path / "nope"))["seen"] == 0
