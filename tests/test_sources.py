"""Request shapes the upstream APIs actually accept.

These are contract tests, not network tests: the transport is stubbed and the
assertions are about what we send. They exist because both endpoints have a
constraint that fails quietly rather than loudly — ListenBrainz rejects a
window bounded from both ends, and Last.fm's `from` is a lower bound that will
silently drop a play if you advance the cursor past it.
"""

from app.sources import lastfm, listenbrainz


def listen(played_at: int, artist: str = "A", title: str = "One") -> dict:
    return {
        "listened_at": played_at,
        "track_metadata": {"artist_name": artist, "track_name": title, "release_name": ""},
    }


# ─── ListenBrainz ──────────────────────────────────────────────────────────


def test_listenbrainz_never_sends_max_ts_and_min_ts_together(monkeypatch):
    """The endpoint accepts one bound or the other, never both in one call."""
    calls = []

    def fake_get(path, params):
        calls.append(params)
        if len(calls) > 3:
            return {"payload": {"listens": []}}
        newest = 10_000 - (len(calls) - 1) * 100
        return {"payload": {"listens": [listen(newest - i) for i in range(100)]}}

    monkeypatch.setattr(listenbrainz, "_get", fake_get)
    monkeypatch.setattr(listenbrainz.config, "LISTENBRAINZ_USER", "someone")

    list(listenbrainz.recent_tracks(since=5_000))

    assert calls, "expected at least one request"
    for params in calls:
        assert not ("max_ts" in params and "min_ts" in params), params
    assert "min_ts" not in calls[0]


def test_listenbrainz_first_page_is_unbounded(monkeypatch):
    monkeypatch.setattr(listenbrainz, "_get", lambda p, params: {"payload": {"listens": []}})
    monkeypatch.setattr(listenbrainz.config, "LISTENBRAINZ_USER", "someone")

    list(listenbrainz.recent_tracks(since=1_000))
    # Nothing to assert beyond "it did not raise"; the shape check is below.


def test_listenbrainz_stops_at_the_cursor(monkeypatch):
    page = [listen(1_200), listen(1_100), listen(900), listen(800)]
    monkeypatch.setattr(listenbrainz, "_get", lambda p, params: {"payload": {"listens": page}})
    monkeypatch.setattr(listenbrainz.config, "LISTENBRAINZ_USER", "someone")

    plays = list(listenbrainz.recent_tracks(since=1_000))
    assert [p["played_at"] for p in plays] == [1_200, 1_100]


def test_listenbrainz_walks_backwards_with_max_ts(monkeypatch):
    pages = [
        [listen(9_000 - i) for i in range(100)],
        [listen(8_000 - i) for i in range(3)],
    ]
    seen = []

    def fake_get(path, params):
        seen.append(params.get("max_ts"))
        return {"payload": {"listens": pages[len(seen) - 1] if len(seen) <= len(pages) else []}}

    monkeypatch.setattr(listenbrainz, "_get", fake_get)
    monkeypatch.setattr(listenbrainz.config, "LISTENBRAINZ_USER", "someone")

    plays = list(listenbrainz.recent_tracks(since=0))
    assert seen[0] is None
    assert seen[1] == 9_000 - 99  # the oldest of page one, exclusive
    assert len(plays) == 103


def test_listenbrainz_ignores_entries_without_a_timestamp(monkeypatch):
    page = [{"listened_at": 0, "track_metadata": {"artist_name": "A", "track_name": "B"}}]
    monkeypatch.setattr(listenbrainz, "_get", lambda p, params: {"payload": {"listens": page}})
    monkeypatch.setattr(listenbrainz.config, "LISTENBRAINZ_USER", "someone")

    assert list(listenbrainz.recent_tracks()) == []


# ─── Last.fm ───────────────────────────────────────────────────────────────


def lastfm_page(*entries, total_pages: int = 1) -> dict:
    return {"recenttracks": {"track": list(entries), "@attr": {"totalPages": total_pages}}}


def scrobble(played_at: int, artist: str = "A", title: str = "One") -> dict:
    return {
        "name": title,
        "artist": {"#text": artist},
        "album": {"#text": ""},
        "date": {"uts": str(played_at)},
    }


def test_lastfm_from_is_the_cursor_not_cursor_plus_one(monkeypatch):
    """`from` is a lower bound — advancing past it would drop a boundary play."""
    captured = {}

    def fake_get(method, params):
        captured.update(params)
        return lastfm_page()

    monkeypatch.setattr(lastfm, "_get", fake_get)
    monkeypatch.setattr(lastfm.config, "LASTFM_USER", "someone")

    list(lastfm.recent_tracks(since=1_700_000_000))
    assert captured["from"] == 1_700_000_000


def test_lastfm_omits_from_on_a_first_sync(monkeypatch):
    captured = {}
    monkeypatch.setattr(lastfm, "_get", lambda m, p: (captured.update(p), lastfm_page())[1])
    monkeypatch.setattr(lastfm.config, "LASTFM_USER", "someone")

    list(lastfm.recent_tracks(since=0))
    assert captured["from"] is None


def test_lastfm_skips_the_now_playing_track(monkeypatch):
    now_playing = {
        "name": "Playing", "artist": {"#text": "A"}, "@attr": {"nowplaying": "true"},
    }
    page = lastfm_page(now_playing, scrobble(1_000, title="Finished"))
    monkeypatch.setattr(lastfm, "_get", lambda m, p: page)
    monkeypatch.setattr(lastfm.config, "LASTFM_USER", "someone")

    plays = list(lastfm.recent_tracks())
    assert [p["title"] for p in plays] == ["Finished"]


def test_lastfm_follows_pagination(monkeypatch):
    pages = [
        lastfm_page(scrobble(2_000), total_pages=2),
        lastfm_page(scrobble(1_000), total_pages=2),
    ]
    monkeypatch.setattr(lastfm, "_get", lambda m, p: pages[p["page"] - 1])
    monkeypatch.setattr(lastfm.config, "LASTFM_USER", "someone")

    assert len(list(lastfm.recent_tracks())) == 2


def test_lastfm_handles_a_single_track_returned_as_an_object(monkeypatch):
    """Last.fm collapses a one-item list into a bare object."""
    page = {"recenttracks": {"track": scrobble(1_000), "@attr": {"totalPages": 1}}}
    monkeypatch.setattr(lastfm, "_get", lambda m, p: page)
    monkeypatch.setattr(lastfm.config, "LASTFM_USER", "someone")

    assert len(list(lastfm.recent_tracks())) == 1
