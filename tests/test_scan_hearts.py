"""The hearts, where they meet the scan.

Two halves are tested here. The prompt half is about *ordering* as much as
content — a model reading a long prompt weights the top of it more heavily, and
the entire premise of this feature is that fifty deliberate hearts outrank fifty
thousand plays. The storing half is about the blend surviving the round trip
intact, so a card can say which part of its score was the model and which part
was the listener.
"""

from __future__ import annotations

import pytest

from app import affinity, db, scan
from app.sources import navidrome
from tests.test_scan import recommendations, run_scan  # noqa: F401  (fixtures come with it)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Enrichment passes values through, carrying tags so genres can be scored."""
    monkeypatch.setattr(
        scan,
        "enrich",
        lambda item: {
            "artist": item["artist"],
            "title": item["title"],
            "album": item.get("album", ""),
            "year": "",
            "track_no": 0,
            "duration": 0,
            "recording_mbid": "",
            "cover_url": "",
            "tags": item.get("tags", []),
        },
    )


def base_profile(**extra) -> dict:
    return {
        "days": 90, "plays": 120, "artists": 8,
        "top_artists": [{"artist": "Radiohead", "plays": 40}],
        "top_tracks": [{"artist": "Radiohead", "title": "Karma Police", "plays": 12}],
        "recent_discoveries": [{"artist": "Portishead"}],
        **extra,
    }


# ─── The prompt ────────────────────────────────────────────────────────────


def test_a_profile_without_navidrome_builds_the_prompt_it_always_did():
    prompt = scan.build_prompt(base_profile(), [], 40)

    assert "HEARTS" not in prompt
    assert "MOST PLAYED ARTISTS" in prompt


def test_hearted_artists_and_tracks_reach_the_prompt():
    prompt = scan.build_prompt(
        base_profile(
            loved_artists=[{"artist": "Aphex Twin", "hearts": 4}],
            loved_tracks=[{"artist": "Aphex Twin", "title": "Xtal", "starred_at": 1}],
        ),
        [], 40,
    )

    assert "Aphex Twin (4 hearted)" in prompt
    assert "Aphex Twin — Xtal" in prompt


def test_the_hearts_come_before_the_plays():
    """Position is weight. The scarce signal goes where the model reads hardest."""
    prompt = scan.build_prompt(
        base_profile(loved_artists=[{"artist": "Aphex Twin", "hearts": 4}]), [], 40
    )

    assert prompt.index("HEARTS") < prompt.index("MOST PLAYED ARTISTS")


def test_the_prompt_says_which_signal_is_the_stronger_one():
    prompt = scan.build_prompt(
        base_profile(loved_artists=[{"artist": "Aphex Twin", "hearts": 4}]), [], 40
    )
    assert "strongest signal" in prompt


def test_hearted_genres_reach_the_prompt():
    prompt = scan.build_prompt(
        base_profile(loved_genres=[{"genre": "idm", "hearts": 9}]), [], 40
    )
    assert "idm (9)" in prompt


def test_library_play_counts_are_labelled_as_not_scrobbled():
    prompt = scan.build_prompt(
        base_profile(
            library_top_tracks=[{"artist": "Burial", "title": "Archangel", "play_count": 41}]
        ),
        [], 40,
    )

    assert "Burial — Archangel (41)" in prompt
    assert "rather than scrobbled" in prompt


def test_the_hearted_sections_are_bounded():
    """A listener with two thousand hearts must not blow out the context window."""
    prompt = scan.build_prompt(
        base_profile(
            loved_tracks=[{"artist": f"A{i}", "title": f"T{i}"} for i in range(2000)],
            loved_artists=[{"artist": f"A{i}", "hearts": 3} for i in range(2000)],
        ),
        [], 40,
    )

    assert prompt.count(" — T") == scan.LOVED_TRACKS_IN_PROMPT
    assert "A1999" not in prompt


def test_the_system_prompt_distinguishes_the_two_kinds_of_evidence():
    assert "HEARTS" in scan.SYSTEM_PROMPT and "PLAYS" in scan.SYSTEM_PROMPT


# ─── The profile query ─────────────────────────────────────────────────────


def test_the_profile_is_unchanged_without_navidrome(play):
    from app import history

    play("Radiohead", "Karma Police")
    profile = history.profile(days=90)

    assert profile["loved_tracks"] == [] and profile["loved_artists"] == []


def test_the_profile_reads_hearts_newest_first(
    navidrome_credentials, navidrome_track, play
):
    from app import history

    play("Radiohead", "Karma Police")
    navidrome_track("Aphex Twin", "Xtal", starred=True, starred_at=1000)
    navidrome_track("Burial", "Archangel", starred=True, starred_at=9000)

    profile = history.profile(days=90)

    assert [t["title"] for t in profile["loved_tracks"]] == ["Archangel", "Xtal"]


def test_the_profile_ignores_tracks_that_are_merely_owned(
    navidrome_credentials, navidrome_track, play
):
    from app import history

    play("Radiohead", "Karma Police")
    navidrome_track("Burial", "Archangel", starred=False, play_count=40)

    profile = history.profile(days=90)

    assert profile["loved_tracks"] == []
    assert [t["title"] for t in profile["library_top_tracks"]] == ["Archangel"]


# ─── Storing the blend ─────────────────────────────────────────────────────


def test_a_scan_without_navidrome_stores_the_model_score_untouched(monkeypatch, play):
    run_scan(monkeypatch, recommendations(("Portishead", "Glory Box")), play)

    with db.connect() as conn:
        row = conn.execute("SELECT * FROM suggestions").fetchone()
    assert row["match"] == 90 and row["match_base"] == 90
    assert row["affinity"] == 0 and row["affinity_reason"] == ""


def test_a_hearted_artist_lifts_the_stored_match(
    monkeypatch, play, navidrome_credentials, navidrome_track
):
    navidrome_track("Portishead", "Roads", starred=True)
    monkeypatch.setattr(scan.history, "sync_navidrome", lambda *a, **k: {})

    run_scan(monkeypatch, recommendations(("Portishead", "Glory Box"), match=70), play)

    with db.connect() as conn:
        row = conn.execute("SELECT * FROM suggestions").fetchone()
    assert row["match"] == 70 + affinity.LOVED_ARTIST
    assert row["match_base"] == 70
    assert row["affinity"] == affinity.LOVED_ARTIST
    assert "Portishead" in row["affinity_reason"]


def test_the_boost_is_scored_against_the_enriched_artist(
    monkeypatch, play, navidrome_credentials, navidrome_track
):
    """MusicBrainz's spelling is the one that will match what is on disk."""
    navidrome_track("Portishead", "Roads", starred=True)
    monkeypatch.setattr(scan.history, "sync_navidrome", lambda *a, **k: {})
    monkeypatch.setattr(
        scan, "enrich",
        lambda item: {
            "artist": "Portishead", "title": item["title"], "album": "", "year": "",
            "track_no": 0, "duration": 0, "recording_mbid": "", "cover_url": "", "tags": [],
        },
    )

    run_scan(monkeypatch, recommendations(("portis head", "Glory Box"), match=70), play)

    with db.connect() as conn:
        row = conn.execute("SELECT affinity FROM suggestions").fetchone()
    assert row["affinity"] == affinity.LOVED_ARTIST


def test_a_hearted_track_is_never_suggested_back(
    monkeypatch, play, navidrome_credentials, navidrome_track
):
    navidrome_track("Portishead", "Glory Box", starred=True)
    monkeypatch.setattr(scan.history, "sync_navidrome", lambda *a, **k: {})

    result = run_scan(monkeypatch, recommendations(("Portishead", "Glory Box")), play)

    assert result["returned"] == 1 and result["kept"] == 0


def test_the_scan_syncs_navidrome_when_it_is_configured(
    monkeypatch, play, navidrome_credentials
):
    calls = []
    monkeypatch.setattr(scan.history, "sync_navidrome", lambda *a, **k: calls.append(1))

    run_scan(monkeypatch, recommendations(("Portishead", "Glory Box")), play)

    assert calls == [1]


def test_the_scan_does_not_sync_navidrome_when_it_is_not_configured(monkeypatch, play):
    calls = []
    monkeypatch.setattr(scan.history, "sync_navidrome", lambda *a, **k: calls.append(1))

    run_scan(monkeypatch, recommendations(("Portishead", "Glory Box")), play)

    assert calls == []


def test_a_navidrome_outage_does_not_fail_the_scan(
    monkeypatch, play, navidrome_credentials
):
    """The recommender worked without this signal before and still does."""
    def boom(*args, **kwargs):
        raise navidrome.NavidromeError("connection refused")

    monkeypatch.setattr(navidrome, "starred_songs", boom)
    monkeypatch.setattr(navidrome, "library_songs", boom)

    result = run_scan(monkeypatch, recommendations(("Portishead", "Glory Box")), play)

    assert result["kept"] == 1


def test_the_card_api_carries_the_breakdown(client, suggestion):
    suggestion("Portishead", "Glory Box", match=82)
    with db.connect() as conn:
        conn.execute(
            "UPDATE suggestions SET match_base = 70, affinity = 12, "
            "affinity_reason = 'you have hearted 3 tracks by Portishead'"
        )

    card = client.get("/api/suggestions").json()["suggestions"][0]

    assert card["match"] == 82 and card["match_base"] == 70
    assert card["affinity"] == 12
    assert "3 tracks by Portishead" in card["affinity_reason"]
