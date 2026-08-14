"""The scan pipeline, with the network stubbed out."""

import pytest

from app import ai, db, scan
from app.norm import track_key


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Enrichment passes values through unchanged unless a test says otherwise."""
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
            "tags": [],
        },
    )


def recommendations(*pairs, match=90):
    return [
        {"artist": artist, "title": title, "match": match, "reason": "because", "seed": "seed"}
        for artist, title in pairs
    ]


def stub_ai(monkeypatch, items):
    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(scan.ai, "available", lambda: True)
    monkeypatch.setattr(scan.ai, "complete_json", lambda *a, **k: {"recommendations": items})


def run_scan(monkeypatch, items, play):
    play("Radiohead", "Karma Police")
    monkeypatch.setattr(scan.history, "sync", lambda *a, **k: {"added": 0, "sources": {}})
    monkeypatch.setattr(scan.exclude, "scan_library", lambda *a, **k: {"seen": 0})
    monkeypatch.setattr(scan.history, "configured", lambda: True)
    stub_ai(monkeypatch, items)
    return scan.run("test")


# ─── Response shapes ───────────────────────────────────────────────────────


def test_a_bare_list_is_accepted():
    assert scan._recommendations([{"artist": "A"}]) == [{"artist": "A"}]


@pytest.mark.parametrize("key", ["recommendations", "tracks", "results", "items"])
def test_the_wrapper_key_models_actually_use_is_accepted(key):
    assert scan._recommendations({key: [{"artist": "A"}]}) == [{"artist": "A"}]


def test_an_unrecognised_shape_yields_nothing():
    assert scan._recommendations({"nope": 1}) == []
    assert scan._recommendations("text") == []


def test_an_object_keyed_by_the_track_is_unpacked():
    """What llama3.1:8b answers when it is only told "valid JSON": a dictionary
    keyed by "Artist — Title", with the fields under names of its own choosing.
    This used to yield nothing at all and fail the scan."""
    answer = {
        "Gareth Emery — Laserface 01 (Aperture)": {
            "artist": "Gareth Emery",
            "name": "Laserface 01 (Aperture)",
            "genre": "Electronic",
        }
    }
    assert scan._recommendations(answer) == [
        {
            "artist": "Gareth Emery",
            "name": "Laserface 01 (Aperture)",
            "title": "Laserface 01 (Aperture)",
            "genre": "Electronic",
        }
    ]


def test_a_keyed_object_supplies_what_the_entry_omits():
    answer = {"Pendulum — Propane Nightmares": {"match": 88}}
    assert scan._recommendations(answer) == [
        {"artist": "Pendulum", "title": "Propane Nightmares", "match": 88}
    ]


def test_bare_strings_are_read_as_artist_and_title():
    assert scan._recommendations(["Portishead - Glory Box"]) == [
        {"artist": "Portishead", "title": "Glory Box"}
    ]


def test_a_hyphenated_name_is_not_split_down_the_middle():
    """The separator needs its spaces: "Jay-Z" is one artist, not two fields."""
    assert scan._recommendations(["Jay-Z — 99 Problems"]) == [
        {"artist": "Jay-Z", "title": "99 Problems"}
    ]


def test_junk_in_the_list_is_dropped_rather_than_crashing():
    """A string, a null and a number alongside real entries. These reached
    _store's item.get() and took the whole scan down with an AttributeError."""
    assert scan._recommendations([{"artist": "A", "title": "One"}, None, 7, []]) == [
        {"artist": "A", "title": "One"}
    ]


def test_a_list_of_artists_becomes_one_credit():
    assert scan._recommendations([{"artists": ["Above & Beyond", "Zoë Johnston"],
                                   "track": "Alchemy"}]) == [
        {
            "artists": ["Above & Beyond", "Zoë Johnston"],
            "artist": "Above & Beyond, Zoë Johnston",
            "track": "Alchemy",
            "title": "Alchemy",
        }
    ]


def test_what_the_entry_says_beats_what_it_is_filed_under():
    answer = {"Wrong Label — Wrong Title": {"artist": "Boards of Canada", "title": "Roygbiv"}}
    assert scan._recommendations(answer) == [
        {"artist": "Boards of Canada", "title": "Roygbiv"}
    ]


# ─── Confidence ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        (95, 95), ("95", 95), ("95%", 95), (0.87, 87), ("0.87", 87),
        (1, 1), (1.0, 100), (250, 100), (-5, 0), ("very high", 0), (None, 0), (True, 0),
    ],
)
def test_a_confidence_is_read_however_it_is_written(value, expected):
    assert scan._match_percent(value) == expected


# ─── Storing ───────────────────────────────────────────────────────────────


def test_a_scan_stores_what_it_is_given(monkeypatch, play):
    result = run_scan(monkeypatch, recommendations(("Portishead", "Glory Box")), play)

    assert result["kept"] == 1
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM suggestions").fetchone()
    assert row["artist"] == "Portishead"
    assert row["status"] == "new"
    assert row["match"] == 90


def test_already_played_tracks_are_dropped(monkeypatch, play):
    result = run_scan(
        monkeypatch,
        recommendations(("Radiohead", "Karma Police"), ("Portishead", "Glory Box")),
        play,
    )
    assert result["returned"] == 2
    assert result["kept"] == 1


def test_at_most_two_tracks_per_artist_are_kept(monkeypatch, play):
    result = run_scan(
        monkeypatch,
        recommendations(("Blur", "One"), ("Blur", "Two"), ("Blur", "Three")),
        play,
    )
    assert result["kept"] == 2


def test_duplicates_inside_one_batch_are_collapsed(monkeypatch, play):
    result = run_scan(
        monkeypatch,
        recommendations(("Portishead", "Glory Box"), ("Portishead", "Glory Box (Remastered)")),
        play,
    )
    assert result["kept"] == 1


def test_an_out_of_range_match_is_clamped(monkeypatch, play):
    run_scan(monkeypatch, recommendations(("A", "One"), match=250), play)
    with db.connect() as conn:
        assert conn.execute("SELECT match FROM suggestions").fetchone()["match"] == 100


def test_a_non_numeric_match_becomes_zero(monkeypatch, play):
    items = [{"artist": "A", "title": "One", "match": "very high", "reason": ""}]
    run_scan(monkeypatch, items, play)
    with db.connect() as conn:
        assert conn.execute("SELECT match FROM suggestions").fetchone()["match"] == 0


def test_entries_missing_an_artist_or_title_are_skipped(monkeypatch, play):
    items = [{"artist": "", "title": "One", "match": 90}, {"artist": "A", "title": "", "match": 90}]
    result = run_scan(monkeypatch, items, play)
    assert result["kept"] == 0


def test_a_hidden_track_is_never_re_suggested(monkeypatch, play, suggestion):
    suggestion("Portishead", "Glory Box", status="hidden")
    result = run_scan(monkeypatch, recommendations(("Portishead", "Glory Box")), play)

    assert result["kept"] == 0
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status FROM suggestions WHERE track_key = ?",
            (track_key("Portishead", "Glory Box"),),
        ).fetchone()
    assert row["status"] == "hidden"


# ─── Failure paths ─────────────────────────────────────────────────────────


def test_a_scan_without_an_ai_backend_fails_loudly(monkeypatch, play):
    play("Radiohead", "Karma Police")
    monkeypatch.setattr(scan.ai, "available", lambda: False)

    with pytest.raises(RuntimeError, match="not configured"):
        scan.run("test")

    with db.connect() as conn:
        row = conn.execute("SELECT status, error FROM scans ORDER BY id DESC").fetchone()
    assert row["status"] == "failed"
    assert "not configured" in row["error"]


def test_a_scan_without_history_fails_before_calling_the_model(monkeypatch):
    monkeypatch.setattr(scan.ai, "available", lambda: True)
    monkeypatch.setattr(scan.history, "configured", lambda: False)

    with pytest.raises(RuntimeError, match="no listening history"):
        scan.run("test")


def test_a_scan_with_no_recent_plays_says_so(monkeypatch, play):
    play("Old", "Song", at=db.now() - 400 * 86400)
    monkeypatch.setattr(scan.history, "sync", lambda *a, **k: {})
    monkeypatch.setattr(scan.exclude, "scan_library", lambda *a, **k: {})
    monkeypatch.setattr(scan.history, "configured", lambda: True)
    monkeypatch.setattr(scan.ai, "available", lambda: True)

    with pytest.raises(RuntimeError, match="no plays"):
        scan.run("test")


def test_the_scan_lock_is_released_after_a_failure(monkeypatch):
    monkeypatch.setattr(scan.ai, "available", lambda: False)
    monkeypatch.setattr(scan.history, "configured", lambda: True)

    for _ in range(2):
        with pytest.raises(RuntimeError):
            scan.run("test")
    assert scan.state()["running"] is False


# ─── Retention ─────────────────────────────────────────────────────────────


def test_purge_drops_stale_new_cards_but_keeps_decisions(suggestion):
    old = db.now() - 90 * 86400
    for status in ("new", "saved", "hidden", "downloaded"):
        suggestion_id = suggestion(f"Artist {status}", "Song", status=status)
        with db.connect() as conn:
            conn.execute("UPDATE suggestions SET created_at = ? WHERE id = ?", (old, suggestion_id))

    scan._purge(60)

    with db.connect() as conn:
        remaining = {row["status"] for row in conn.execute("SELECT status FROM suggestions")}
    assert remaining == {"saved", "hidden", "downloaded"}


# ─── Prompt ────────────────────────────────────────────────────────────────


def test_the_prompt_carries_the_profile_and_the_exclusions():
    profile = {
        "days": 90, "plays": 120, "artists": 8,
        "top_artists": [{"artist": "Radiohead", "plays": 40}],
        "top_tracks": [{"artist": "Radiohead", "title": "Karma Police", "plays": 12}],
        "recent_discoveries": [{"artist": "Portishead"}],
    }
    prompt = scan.build_prompt(profile, ["Radiohead — Karma Police"], 40)

    assert "Radiohead (40)" in prompt
    assert "Portishead" in prompt
    assert "EXCLUDE" in prompt
    assert "Return exactly 40 recommendations" in prompt
