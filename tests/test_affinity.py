"""The code-side half of the match blend.

The AI is told about the hearts and asked to weigh them; this is what happens
regardless of whether it did. These tests are mostly about the boundaries —
that the boost is bounded, that it cannot fire twice for one fact, and that it
disappears entirely when there is nothing to score against — because an
unbounded or double-counted boost would turn every scan into a list of tracks
by the six artists you happen to have starred.
"""

from __future__ import annotations

from app import affinity


# ─── Nothing configured ────────────────────────────────────────────────────


def test_no_navidrome_means_no_boost():
    picture = affinity.load()
    assert not picture
    assert picture.boost("Aphex Twin") == (0, "")


def test_configured_but_never_synced_means_no_boost(navidrome_credentials):
    assert affinity.load().boost("Aphex Twin", seed="Autechre") == (0, "")


def test_apply_leaves_the_model_score_alone_when_there_is_nothing_to_add():
    assert affinity.apply(72, "Aphex Twin") == {
        "match": 72, "match_base": 72, "affinity": 0, "affinity_reason": "",
    }


# ─── The signals ───────────────────────────────────────────────────────────


def test_a_hearted_artist_is_the_strongest_signal(navidrome_credentials, navidrome_track):
    navidrome_track("Aphex Twin", "Xtal", starred=True)
    navidrome_track("Aphex Twin", "Ageispolis", starred=True)

    points, why = affinity.load().boost("Aphex Twin")

    assert points == affinity.LOVED_ARTIST
    assert "2 tracks by Aphex Twin" in why


def test_one_heart_is_described_in_the_singular(navidrome_credentials, navidrome_track):
    navidrome_track("Aphex Twin", "Xtal", starred=True)
    assert "1 track by Aphex Twin" in affinity.load().boost("Aphex Twin")[1]


def test_a_hearted_seed_artist_counts_for_less(navidrome_credentials, navidrome_track):
    navidrome_track("Aphex Twin", "Xtal", starred=True)

    points, why = affinity.load().boost("Autechre", seed="Aphex Twin")

    assert points == affinity.LOVED_SEED
    assert "came from Aphex Twin" in why


def test_the_seed_does_not_pay_twice_for_the_same_artist(
    navidrome_credentials, navidrome_track
):
    """A model naming the suggestion's own artist as its seed is not evidence."""
    navidrome_track("Aphex Twin", "Xtal", starred=True)

    points, _ = affinity.load().boost("Aphex Twin", seed="Aphex Twin")

    assert points == affinity.LOVED_ARTIST


def test_a_heavily_played_artist_counts(navidrome_credentials, navidrome_track):
    navidrome_track("Burial", "Archangel", play_count=40)

    points, why = affinity.load().boost("Burial")

    assert points == affinity.PLAYED_ARTIST
    assert "most played in Navidrome" in why


def test_a_barely_played_artist_does_not(navidrome_credentials, navidrome_track):
    navidrome_track("Burial", "Archangel", play_count=1)
    assert affinity.load().boost("Burial") == (0, "")


def test_plays_do_not_stack_on_top_of_a_heart(navidrome_credentials, navidrome_track):
    """Both say "this artist"; charging for both would double-count one fact."""
    navidrome_track("Aphex Twin", "Xtal", starred=True, play_count=90)

    points, _ = affinity.load().boost("Aphex Twin")

    assert points == affinity.LOVED_ARTIST


def test_a_hearted_genre_counts(navidrome_credentials, navidrome_track):
    navidrome_track("Aphex Twin", "Xtal", starred=True, genre="IDM")
    navidrome_track("Autechre", "Amber", starred=True, genre="IDM")

    points, why = affinity.load().boost("Someone Else", tags=["idm", "ambient"])

    assert points == affinity.LOVED_GENRE
    assert "you heart idm" in why


def test_a_genre_hearted_once_is_not_a_pattern(navidrome_credentials, navidrome_track):
    navidrome_track("Aphex Twin", "Xtal", starred=True, genre="IDM")
    assert affinity.load().boost("Someone Else", tags=["idm"]) == (0, "")


def test_genre_matching_ignores_case(navidrome_credentials, navidrome_track):
    navidrome_track("A", "One", starred=True, genre="Trip Hop")
    navidrome_track("B", "Two", starred=True, genre="trip hop")

    assert affinity.load().boost("C", tags=["Trip Hop"])[0] == affinity.LOVED_GENRE


def test_unhearted_tracks_contribute_nothing(navidrome_credentials, navidrome_track):
    navidrome_track("Aphex Twin", "Xtal", starred=False, genre="IDM")
    assert affinity.load().boost("Aphex Twin", tags=["idm"]) == (0, "")


# ─── Bounds ────────────────────────────────────────────────────────────────


def test_the_boost_is_capped(navidrome_credentials, navidrome_track):
    """Every signal at once still cannot carry a bad recommendation."""
    for title in ("Xtal", "Ageispolis", "Tha"):
        navidrome_track("Aphex Twin", title, starred=True, genre="IDM", play_count=80)
    navidrome_track("Autechre", "Amber", starred=True, genre="IDM")

    points, _ = affinity.load().boost("Aphex Twin", seed="Autechre", tags=["idm"])

    assert points == affinity.MAX_BOOST
    assert affinity.MAX_BOOST < affinity.LOVED_ARTIST + affinity.LOVED_SEED + affinity.LOVED_GENRE


def test_the_final_match_never_passes_100(navidrome_credentials, navidrome_track):
    navidrome_track("Aphex Twin", "Xtal", starred=True)

    scored = affinity.apply(96, "Aphex Twin")

    assert scored["match"] == 100
    # The parts stay honest about what actually happened.
    assert scored["match_base"] == 96 and scored["affinity"] == affinity.LOVED_ARTIST


def test_the_final_match_never_goes_below_zero(navidrome_credentials):
    assert affinity.apply(-5, "Nobody")["match"] == 0


def test_the_breakdown_is_recorded_for_the_card(navidrome_credentials, navidrome_track):
    navidrome_track("Aphex Twin", "Xtal", starred=True)

    scored = affinity.apply(70, "Aphex Twin")

    assert scored == {
        "match": 70 + affinity.LOVED_ARTIST,
        "match_base": 70,
        "affinity": affinity.LOVED_ARTIST,
        "affinity_reason": "you have hearted 1 track by Aphex Twin",
    }


def test_the_reason_is_bounded(navidrome_credentials, navidrome_track):
    navidrome_track("A" * 500, "One", starred=True)
    assert len(affinity.load().boost("A" * 500)[1]) <= 400


# ─── Name matching ─────────────────────────────────────────────────────────


def test_artists_are_matched_through_the_normalised_key(
    navidrome_credentials, navidrome_track
):
    """The model's spelling and Navidrome's rarely agree character for character."""
    navidrome_track("The Beatles", "Yesterday", starred=True)

    assert affinity.load().boost("Beatles")[0] == affinity.LOVED_ARTIST


def test_a_featured_credit_still_matches(navidrome_credentials, navidrome_track):
    navidrome_track("Burial", "Archangel", starred=True)

    assert affinity.load().boost("Burial feat. Four Tet")[0] == affinity.LOVED_ARTIST


def test_an_unrelated_artist_gets_nothing(navidrome_credentials, navidrome_track):
    navidrome_track("Aphex Twin", "Xtal", starred=True)
    assert affinity.load().boost("Taylor Swift") == (0, "")


def test_a_blank_seed_is_ignored(navidrome_credentials, navidrome_track):
    navidrome_track("Aphex Twin", "Xtal", starred=True)
    assert affinity.load().boost("Autechre", seed="") == (0, "")


def test_blank_tags_are_ignored(navidrome_credentials, navidrome_track):
    navidrome_track("A", "One", starred=True, genre="idm")
    navidrome_track("B", "Two", starred=True, genre="idm")
    assert affinity.load().boost("C", tags=["", "  ", None]) == (0, "")
