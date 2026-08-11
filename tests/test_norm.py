"""Normalisation is what stops the same track being suggested every scan."""

from app.norm import artist_key, safe_filename, title_key, track_key


def test_leading_the_is_ignored():
    assert artist_key("The Beatles") == artist_key("Beatles")


def test_case_accents_and_punctuation_fold_together():
    assert artist_key("Sigur Rós") == artist_key("SIGUR ROS")
    assert artist_key("Godspeed You! Black Emperor") == artist_key("Godspeed You Black Emperor")


def test_ampersand_and_the_word_and_agree():
    assert title_key("Me & You") == title_key("Me and You")


def test_only_the_primary_artist_survives_a_collaboration():
    assert artist_key("Nick Cave & The Bad Seeds") == artist_key("Nick Cave")
    assert artist_key("Tyler, The Creator") == artist_key("Tyler")
    assert artist_key("Artist A x Artist B") == artist_key("Artist A")


def test_a_trailing_x_is_part_of_the_name_not_a_separator():
    assert artist_key("Malcolm X") == "malcolm x"


def test_featured_credits_are_dropped_in_every_spelling():
    base = artist_key("Kendrick Lamar")
    for spelling in ("Kendrick Lamar feat. SZA", "Kendrick Lamar ft SZA",
                     "Kendrick Lamar (featuring SZA)"):
        assert artist_key(spelling) == base


def test_edition_noise_is_stripped_from_titles():
    base = title_key("Karma Police")
    for spelling in ("Karma Police (Remastered 2016)", "Karma Police [Remastered]",
                     "Karma Police - 2016 Remaster", "Karma Police (Album Version)"):
        assert title_key(spelling) == base


def test_a_live_version_is_not_folded_into_the_studio_one():
    assert title_key("Karma Police (Live at Glastonbury)") != title_key("Karma Police")


def test_track_key_combines_both_sides():
    assert track_key("The Beatles", "Let It Be (Remastered)") == track_key("Beatles", "Let It Be")
    assert track_key("A", "One") != track_key("A", "Two")


def test_safe_filename_strips_path_characters():
    assert "/" not in safe_filename("AC/DC", "x")
    assert safe_filename("", "fallback") == "fallback"
    assert safe_filename("....", "fallback") == "fallback"
    assert len(safe_filename("z" * 400, "x")) == 120
