"""MusicBrainz as the genre source when there is no Last.fm key.

Contract tests with the transport stubbed: what is asked for, what is kept of
the answer, and that the fallback only runs when Last.fm is not configured —
for anyone who still has a key, nothing about a scan changes.
"""

from __future__ import annotations

import pytest

from app import config, scan
from app.sources import lastfm, musicbrainz


@pytest.fixture(autouse=True)
def fresh_genre_cache():
    musicbrainz._genre_cache.clear()
    yield
    musicbrainz._genre_cache.clear()


def stub_get(monkeypatch, answer):
    """Replace the HTTP call; ``answer`` is a body or an exception to raise."""
    calls: list[tuple[str, dict]] = []

    def fake_get(path, params):
        calls.append((path, dict(params)))
        if isinstance(answer, Exception):
            raise answer
        return answer(path, params) if callable(answer) else answer

    monkeypatch.setattr(musicbrainz, "_get", fake_get)
    return calls


ARTIST_MBID = "a74b1b7f-71a5-4011-9441-d0b5e4122711"


def recording(**extra) -> dict:
    """A recording search hit, as MusicBrainz returns one."""
    return {
        "id": "rec-1",
        "score": 100,
        "title": "Karma Police",
        "length": 264000,
        "artist-credit": [
            {"name": "Radiohead", "joinphrase": "",
             "artist": {"id": ARTIST_MBID, "name": "Radiohead", "sort-name": "Radiohead"}},
        ],
        "releases": [{
            "id": "rel-1", "title": "OK Computer", "date": "1997-05-21",
            "release-group": {"primary-type": "Album", "secondary-types": []},
            "media": [{"tracks": [{"position": 6}]}],
        }],
        **extra,
    }


# ─── resolve_track ─────────────────────────────────────────────────────────


def test_resolve_track_returns_the_lead_artists_mbid(monkeypatch):
    stub_get(monkeypatch, {"recordings": [recording()]})

    resolved = musicbrainz.resolve_track("Radiohead", "Karma Police")
    assert resolved["artist_mbid"] == ARTIST_MBID


def test_a_collaboration_is_filed_under_its_first_credit(monkeypatch):
    hit = recording()
    hit["artist-credit"] = [
        {"name": "Daft Punk", "joinphrase": " feat. ", "artist": {"id": "dp", "name": "Daft Punk"}},
        {"name": "Pharrell Williams", "joinphrase": "", "artist": {"id": "pw", "name": "Pharrell Williams"}},
    ]
    stub_get(monkeypatch, {"recordings": [hit]})

    assert musicbrainz.resolve_track("Daft Punk", "Get Lucky")["artist_mbid"] == "dp"


# ─── artist_genres ─────────────────────────────────────────────────────────


def test_genres_are_requested_for_the_artist(monkeypatch):
    calls = stub_get(monkeypatch, {"genres": [{"name": "alternative rock", "count": 9}]})

    assert musicbrainz.artist_genres(ARTIST_MBID) == ["alternative rock"]
    assert calls == [(f"artist/{ARTIST_MBID}", {"inc": "genres"})]


def test_the_most_voted_come_first_and_only_five_are_kept(monkeypatch):
    stub_get(monkeypatch, {"genres": [
        {"name": "rock", "count": 3},
        {"name": "art rock", "count": 12},
        {"name": "alternative rock", "count": 20},
        {"name": "experimental", "count": 3},
        {"name": "electronic", "count": 5},
        {"name": "britpop", "count": 1},
    ]})

    assert musicbrainz.artist_genres(ARTIST_MBID) == [
        "alternative rock", "art rock", "electronic", "rock", "experimental",
    ]


def test_genres_voted_down_to_nothing_are_dropped(monkeypatch):
    stub_get(monkeypatch, {"genres": [
        {"name": "trip hop", "count": 4},
        {"name": "polka", "count": 0},
        {"name": "vaporwave", "count": -2},
        {"name": "", "count": 9},
        "not a genre",
    ]})

    assert musicbrainz.artist_genres(ARTIST_MBID) == ["trip hop"]


def test_an_artist_with_no_genres_gets_none(monkeypatch):
    stub_get(monkeypatch, {"id": ARTIST_MBID, "name": "Nobody Tagged Me"})
    assert musicbrainz.artist_genres(ARTIST_MBID) == []


def test_an_artist_is_only_asked_about_once(monkeypatch):
    calls = stub_get(monkeypatch, {"genres": [{"name": "trip hop", "count": 4}]})

    musicbrainz.artist_genres(ARTIST_MBID)
    musicbrainz.artist_genres(ARTIST_MBID)
    assert len(calls) == 1


def test_an_empty_answer_is_remembered_too(monkeypatch):
    calls = stub_get(monkeypatch, {"genres": []})

    musicbrainz.artist_genres(ARTIST_MBID)
    musicbrainz.artist_genres(ARTIST_MBID)
    assert len(calls) == 1


def test_a_failed_lookup_is_retried_rather_than_remembered(monkeypatch):
    calls = stub_get(monkeypatch, musicbrainz.MusicBrainzError("HTTP 503"))

    assert musicbrainz.artist_genres(ARTIST_MBID) == []
    musicbrainz.artist_genres(ARTIST_MBID)
    assert len(calls) == 2


def test_a_body_that_is_not_an_artist_costs_the_tags_not_the_scan(monkeypatch):
    stub_get(monkeypatch, ["not", "an", "artist"])
    assert musicbrainz.artist_genres(ARTIST_MBID) == []


def test_the_cached_list_cannot_be_changed_by_a_caller(monkeypatch):
    stub_get(monkeypatch, {"genres": [{"name": "trip hop", "count": 4}]})

    musicbrainz.artist_genres(ARTIST_MBID).append("polka")
    assert musicbrainz.artist_genres(ARTIST_MBID) == ["trip hop"]


def test_no_mbid_means_no_request(monkeypatch):
    calls = stub_get(monkeypatch, {"genres": [{"name": "rock", "count": 1}]})
    assert musicbrainz.artist_genres("") == []
    assert calls == []


def test_the_request_goes_through_the_throttle(monkeypatch):
    """One request a second is MusicBrainz's rule, and the genre call is no exception."""
    waits = []
    monkeypatch.setattr(musicbrainz, "_throttle", lambda: waits.append(1))

    class Answer:
        status_code = 200

        @staticmethod
        def json():
            return {"genres": []}

    monkeypatch.setattr(musicbrainz.net, "with_retry", lambda fetch, **kw: Answer())

    musicbrainz.artist_genres(ARTIST_MBID)
    assert waits == [1]


# ─── enrich ────────────────────────────────────────────────────────────────


def resolved(**extra) -> dict:
    return {
        "artist": "Radiohead", "title": "Karma Police", "album": "OK Computer",
        "year": "1997", "track": 6, "duration": 264, "recording_mbid": "rec-1",
        "release_mbid": "rel-1", "artist_mbid": ARTIST_MBID, **extra,
    }


def test_without_lastfm_the_genres_come_from_musicbrainz(monkeypatch):
    monkeypatch.setattr(config, "LASTFM_API_KEY", "")
    monkeypatch.setattr(musicbrainz, "resolve_track", lambda *a, **k: resolved())
    asked = []
    monkeypatch.setattr(
        musicbrainz, "artist_genres", lambda mbid: asked.append(mbid) or ["alternative rock"]
    )

    card = scan.enrich({"artist": "Radiohead", "title": "Karma Police"})

    assert card["tags"] == ["alternative rock"]
    assert asked == [ARTIST_MBID]
    # Cover art never depended on Last.fm being there.
    assert card["cover_url"] == musicbrainz.cover_url("rel-1")


def test_with_lastfm_nothing_changes(monkeypatch):
    monkeypatch.setattr(config, "LASTFM_API_KEY", "key")
    monkeypatch.setattr(config, "LASTFM_USER", "someone")
    monkeypatch.setattr(musicbrainz, "resolve_track", lambda *a, **k: resolved())
    monkeypatch.setattr(lastfm, "track_info", lambda artist, title: {
        "artist": artist, "title": title, "album": "", "cover_url": "",
        "tags": ["radiohead", "alternative"], "listeners": 1, "duration": 0,
    })

    def must_not_be_called(mbid):
        raise AssertionError("MusicBrainz genres were looked up with Last.fm configured")

    monkeypatch.setattr(musicbrainz, "artist_genres", must_not_be_called)

    assert scan.enrich({"artist": "Radiohead", "title": "Karma Police"})["tags"] == [
        "radiohead", "alternative",
    ]


def test_an_unresolved_track_is_not_looked_up_by_name(monkeypatch):
    """No recording means no artist MBID, and guessing one by name is how a
    tribute band's genres end up on the original's card."""
    monkeypatch.setattr(config, "LASTFM_API_KEY", "")
    monkeypatch.setattr(musicbrainz, "resolve_track", lambda *a, **k: None)
    monkeypatch.setattr(musicbrainz, "artist_genres", lambda mbid: pytest.fail("looked up"))

    assert scan.enrich({"artist": "Nobody", "title": "Nothing"})["tags"] == []
