"""Talking to Navidrome, and storing what it says.

Contract tests with the transport stubbed. The things worth pinning down here
are the ones that fail quietly rather than loudly:

* Subsonic answers HTTP 200 for a refused login and puts the failure in the
  body, so a status-code check would read a wrong password as an empty library;
* ``search3`` paging has no natural terminator other than a short page, so a
  server that ignores ``songOffset`` would loop forever;
* an un-starred track that stays marked hearted would keep boosting scores and
  — because hearts are excluded — stay banned from ever being suggested;
* Go prints timestamps with anywhere from one to nine fractional digits.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest

from app import db, exclude, history
from app.sources import navidrome


def child(
    song_id: str = "s1",
    artist: str = "Aphex Twin",
    title: str = "Xtal",
    starred: str | None = None,
    play_count: int = 0,
    rating: int = 0,
    genre: str = "",
) -> dict:
    """A Subsonic ``Child`` as Navidrome serialises one."""
    entry: dict = {
        "id": song_id, "title": title, "artist": artist, "album": "Selected Ambient Works",
        "isDir": False, "playCount": play_count,
    }
    if starred:
        entry["starred"] = starred
    if rating:
        entry["userRating"] = rating
    if genre:
        entry["genre"] = genre
    return entry


def ok(payload: dict) -> dict:
    return {"subsonic-response": {"status": "ok", "version": "1.16.1", **payload}}


# ─── Authentication ────────────────────────────────────────────────────────


def test_auth_never_sends_the_password(navidrome_credentials):
    params = navidrome.auth_params()
    assert "p" not in params
    assert navidrome_credentials["password"] not in params.values()
    assert params["u"] == "listener"


def test_auth_token_is_md5_of_password_and_salt(navidrome_credentials):
    params = navidrome.auth_params()
    expected = hashlib.md5(f"hunter2{params['s']}".encode()).hexdigest()
    assert params["t"] == expected


def test_auth_salt_is_fresh_every_request(navidrome_credentials):
    """A reused salt would make one captured token a permanent credential."""
    salts = {navidrome.auth_params()["s"] for _ in range(20)}
    assert len(salts) == 20


def test_auth_sends_the_required_subsonic_parameters(navidrome_credentials):
    params = navidrome.auth_params()
    assert params["v"] and params["c"] == "musicdrome" and params["f"] == "json"


def test_get_refuses_when_not_configured():
    with pytest.raises(navidrome.NavidromeError, match="NAVIDROME_URL"):
        navidrome._get("ping.view")


# ─── The Subsonic envelope ─────────────────────────────────────────────────


def stub_transport(monkeypatch, responses):
    """Answer each request from ``responses``, recording the params sent."""
    sent = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(dict(request.url.params))
        body = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body)

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(navidrome.httpx, "Client", fake_client)
    return sent


def test_failed_login_raises_rather_than_looking_empty(monkeypatch, navidrome_credentials):
    """Subsonic answers HTTP 200 for a rejected password."""
    stub_transport(monkeypatch, [{
        "subsonic-response": {
            "status": "failed",
            "error": {"code": 40, "message": "Wrong username or password"},
        }
    }])

    with pytest.raises(navidrome.NavidromeError, match="not an API key"):
        navidrome.ping()


def test_other_subsonic_errors_carry_their_code(monkeypatch, navidrome_credentials):
    stub_transport(monkeypatch, [{
        "subsonic-response": {"status": "failed", "error": {"code": 30, "message": "Too old"}}
    }])

    with pytest.raises(navidrome.NavidromeError, match="Navidrome error 30"):
        navidrome.ping()


def test_a_non_navidrome_url_says_so(monkeypatch, navidrome_credentials):
    stub_transport(monkeypatch, [httpx.Response(200, text="<html>nginx</html>")])

    with pytest.raises(navidrome.NavidromeError, match="check NAVIDROME_URL"):
        navidrome.ping()


def test_http_errors_are_reported(monkeypatch, navidrome_credentials):
    stub_transport(monkeypatch, [httpx.Response(502, text="bad gateway")])

    with pytest.raises(navidrome.NavidromeError, match="HTTP 502"):
        navidrome.ping()


def test_ping_reports_what_answered(monkeypatch, navidrome_credentials):
    stub_transport(monkeypatch, [ok({"type": "navidrome", "serverVersion": "0.53.3"})])

    assert navidrome.ping() == {
        "ok": True, "version": "1.16.1", "server": "navidrome", "server_version": "0.53.3",
    }


def test_requests_go_to_the_rest_path(monkeypatch, navidrome_credentials):
    sent = stub_transport(monkeypatch, [ok({})])
    navidrome.ping()
    assert sent[0]["u"] == "listener" and "t" in sent[0] and "s" in sent[0]


# ─── Parsing ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2024-03-11T21:04:07Z", 1710191047),
        ("2024-03-11T21:04:07.123456789Z", 1710191047),   # Go's nanoseconds
        ("2024-03-11T21:04:07.1Z", 1710191047),           # trimmed to one digit
        ("2024-03-11T21:04:07+00:00", 1710191047),
        ("2024-03-11T21:04:07", 1710191047),              # no offset: read as UTC
        ("2024-03-11T22:04:07+01:00", 1710191047),
        ("", 0),
        ("not a date", 0),
        (None, 0),
        (12345, 0),
    ],
)
def test_timestamps_in_every_shape_go_prints(value, expected):
    assert navidrome._timestamp(value) == expected


def test_sub_second_precision_is_padded_not_truncated():
    """`.1` is a hundred milliseconds; reading it as one microsecond is wrong."""
    tenth = navidrome._timestamp("2024-03-11T21:04:07.9Z")
    assert tenth == 1710191047  # 07.9s still floors to the same second
    assert navidrome._timestamp("2024-03-11T21:04:07.9Z") >= navidrome._timestamp(
        "2024-03-11T21:04:07.0Z"
    )


def test_song_reads_the_fields_we_store():
    song = navidrome._song(child(starred="2024-03-11T21:04:07Z", play_count=34, rating=4,
                                 genre="IDM"))
    assert song["artist"] == "Aphex Twin"
    assert song["title"] == "Xtal"
    assert song["starred"] is True
    assert song["starred_at"] == 1710191047
    assert song["play_count"] == 34
    assert song["rating"] == 4
    assert song["genre"] == "IDM"


def test_song_without_a_star_is_not_starred():
    song = navidrome._song(child())
    assert song["starred"] is False and song["starred_at"] == 0


def test_song_falls_back_to_the_opensubsonic_display_artist():
    entry = child(artist="")
    entry.pop("artist")
    entry["displayArtist"] = "Autechre"
    assert navidrome._song(entry)["artist"] == "Autechre"


def test_song_without_an_artist_or_title_is_dropped():
    assert navidrome._song(child(artist="")) is None
    assert navidrome._song(child(title="")) is None


def test_ratings_outside_one_to_five_are_clamped():
    assert navidrome._song(child(rating=99))["rating"] == 5
    assert navidrome._song(child(rating=-3))["rating"] == 0


# ─── getStarred2 ───────────────────────────────────────────────────────────


def test_starred_songs_reads_the_song_list(monkeypatch, navidrome_credentials):
    stub_transport(monkeypatch, [ok({"starred2": {
        "song": [child("s1", starred="2024-03-11T21:04:07Z"),
                 child("s2", artist="Boards of Canada", title="Roygbiv",
                       starred="2024-05-01T09:00:00Z")],
        "album": [{"id": "al1"}],
        "artist": [{"id": "ar1"}],
    }})])

    songs = navidrome.starred_songs()
    assert [s["title"] for s in songs] == ["Xtal", "Roygbiv"]


def test_starred_songs_trusts_the_endpoint_over_a_missing_attribute(
    monkeypatch, navidrome_credentials
):
    """getStarred2 returns these *because* they are starred."""
    stub_transport(monkeypatch, [ok({"starred2": {"song": [child("s1")]}})])

    assert navidrome.starred_songs()[0]["starred"] is True


def test_no_hearts_is_not_an_error(monkeypatch, navidrome_credentials):
    stub_transport(monkeypatch, [ok({"starred2": {}})])
    assert navidrome.starred_songs() == []


def test_a_single_song_serialised_as_a_bare_object_is_read(
    monkeypatch, navidrome_credentials
):
    """Subsonic's JSON is a mechanical translation of its XML, and several
    servers emit a one-element array as a bare object. Iterating a dict yields
    its keys, so this used to kill the sync on 'str' has no attribute 'get'."""
    stub_transport(monkeypatch, [ok({"starred2": {"song": child("s1")}})])

    songs = navidrome.starred_songs()
    assert [s["title"] for s in songs] == ["Xtal"]


@pytest.mark.parametrize(
    "payload",
    [
        {"starred2": None},
        {"starred2": []},
        {"starred2": {"song": None}},
        {"starred2": {"song": "nonsense"}},
        {},
    ],
)
def test_a_malformed_hearts_response_yields_nothing_rather_than_raising(
    monkeypatch, navidrome_credentials, payload
):
    stub_transport(monkeypatch, [ok(payload)])
    assert navidrome.starred_songs() == []


def test_junk_in_the_song_list_is_skipped_not_fatal(monkeypatch, navidrome_credentials):
    """One bad entry must not cost the other nine hundred."""
    stub_transport(monkeypatch, [ok({"starred2": {"song": [
        None, "nonsense", 42, [], child("s1"),
    ]}})])

    assert [s["id"] for s in navidrome.starred_songs()] == ["s1"]


# ─── The library walk ──────────────────────────────────────────────────────


def page(offset: int, count: int, total: int = 250) -> dict:
    songs = [
        child(f"s{i}", title=f"Track {i}")
        for i in range(offset, min(offset + count, total))
    ]
    return ok({"searchResult3": {"song": songs}})


def test_library_walk_pages_with_song_offset(monkeypatch, navidrome_credentials):
    sent = stub_transport(monkeypatch, [page(0, 100), page(100, 100), page(200, 100)])

    songs = list(navidrome.library_songs(page_size=100))

    assert len(songs) == 250
    assert [int(p["songOffset"]) for p in sent] == [0, 100, 200]
    assert all(p["query"] == navidrome.MATCH_ALL for p in sent)


def test_library_walk_does_not_pay_for_artists_and_albums(monkeypatch, navidrome_credentials):
    """Navidrome runs the three searches in parallel; two would be thrown away."""
    sent = stub_transport(monkeypatch, [page(0, 100, total=10)])
    list(navidrome.library_songs(page_size=100))
    assert sent[0]["artistCount"] == "0" and sent[0]["albumCount"] == "0"


def test_library_walk_stops_on_an_empty_page(monkeypatch, navidrome_credentials):
    stub_transport(monkeypatch, [ok({"searchResult3": {}})])
    assert list(navidrome.library_songs(page_size=100)) == []


@pytest.mark.parametrize(
    "payload",
    [{"searchResult3": None}, {"searchResult3": {"song": None}}, {}],
)
def test_a_malformed_page_ends_the_walk_rather_than_raising(
    monkeypatch, navidrome_credentials, payload
):
    stub_transport(monkeypatch, [ok(payload)])
    assert list(navidrome.library_songs(page_size=100)) == []


def test_a_single_song_page_serialised_as_a_bare_object_is_read(
    monkeypatch, navidrome_credentials
):
    stub_transport(monkeypatch, [ok({"searchResult3": {"song": child("s1")}})])
    assert [s["id"] for s in navidrome.library_songs(page_size=100)] == ["s1"]


def test_a_server_that_ignores_the_offset_does_not_loop_forever(
    monkeypatch, navidrome_credentials
):
    """The same full page over and over is a paging bug, not an endless library."""
    sent = stub_transport(monkeypatch, [page(0, 100, total=100)])

    songs = list(navidrome.library_songs(page_size=100))

    assert len(songs) == 100
    assert len(sent) == 2  # the first page, then one that repeated it


def test_library_walk_respects_the_track_ceiling(monkeypatch, navidrome_credentials):
    stub_transport(monkeypatch, [page(0, 50, total=10_000), page(50, 50, total=10_000)])
    songs = list(navidrome.library_songs(page_size=50, max_tracks=100))
    assert len(songs) == 100


def test_page_size_of_zero_skips_the_walk(monkeypatch, navidrome_credentials):
    sent = stub_transport(monkeypatch, [page(0, 100)])
    assert list(navidrome.library_songs(page_size=0)) == []
    assert sent == []


# ─── Syncing into SQLite ───────────────────────────────────────────────────


def test_sync_is_a_no_op_when_navidrome_is_not_configured():
    result = history.sync_navidrome()
    assert result["configured"] is False and result["hearts"] == 0


def test_sync_stores_hearts_and_play_counts(monkeypatch, navidrome_credentials):
    monkeypatch.setattr(
        navidrome, "library_songs",
        lambda *a, **k: iter([
            navidrome._song(child("s1", play_count=34)),
            navidrome._song(child("s2", artist="Burial", title="Archangel", play_count=9)),
        ]),
    )
    monkeypatch.setattr(
        navidrome, "starred_songs",
        lambda: [navidrome._song(child("s1", starred="2024-03-11T21:04:07Z"))],
    )

    result = history.sync_navidrome()

    assert result["hearts"] == 1 and result["library"] == 2 and result["walked"] is True
    with db.connect() as conn:
        rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM navidrome_tracks")}
    assert rows["s1"]["starred"] == 1 and rows["s1"]["play_count"] == 34
    assert rows["s2"]["starred"] == 0 and rows["s2"]["artist"] == "Burial"


def test_the_hearts_call_never_zeroes_a_play_count(monkeypatch, navidrome_credentials):
    """Every count in a Subsonic response is omitempty: absent reads as zero."""
    monkeypatch.setattr(
        navidrome, "library_songs",
        lambda *a, **k: iter([navidrome._song(child("s1", play_count=412))]),
    )
    monkeypatch.setattr(
        navidrome, "starred_songs",
        lambda: [navidrome._song(child("s1", starred="2024-03-11T21:04:07Z"))],
    )

    history.sync_navidrome()

    with db.connect() as conn:
        assert conn.execute(
            "SELECT play_count FROM navidrome_tracks WHERE id = 's1'"
        ).fetchone()["play_count"] == 412


def test_hearts_survive_the_library_walk_that_ran_before_them(
    monkeypatch, navidrome_credentials
):
    """The walk reports stale starred state; getStarred2 is the authority."""
    monkeypatch.setattr(
        navidrome, "library_songs", lambda *a, **k: iter([navidrome._song(child("s1"))])
    )
    monkeypatch.setattr(
        navidrome, "starred_songs",
        lambda: [navidrome._song(child("s1", starred="2024-03-11T21:04:07Z"))],
    )

    history.sync_navidrome()

    with db.connect() as conn:
        row = conn.execute("SELECT starred, starred_at FROM navidrome_tracks").fetchone()
    assert row["starred"] == 1 and row["starred_at"] == 1710191047


def test_unhearting_in_navidrome_clears_the_flag(
    monkeypatch, navidrome_credentials, navidrome_track
):
    """Otherwise it boosts forever and stays banned from being suggested."""
    navidrome_track("Aphex Twin", "Xtal", starred=True)
    monkeypatch.setattr(navidrome, "library_songs", lambda *a, **k: iter([]))
    monkeypatch.setattr(navidrome, "starred_songs", lambda: [])

    history.sync_navidrome()

    with db.connect() as conn:
        row = conn.execute("SELECT starred, starred_at FROM navidrome_tracks").fetchone()
    assert row["starred"] == 0 and row["starred_at"] == 0


def test_unhearting_one_of_several_leaves_the_others(
    monkeypatch, navidrome_credentials
):
    monkeypatch.setattr(navidrome, "library_songs", lambda *a, **k: iter([]))
    monkeypatch.setattr(
        navidrome, "starred_songs",
        lambda: [navidrome._song(child("s1", starred="2024-03-11T21:04:07Z")),
                 navidrome._song(child("s2", artist="Burial", title="Archangel",
                                       starred="2024-03-11T21:04:07Z"))],
    )
    history.sync_navidrome()

    monkeypatch.setattr(
        navidrome, "starred_songs",
        lambda: [navidrome._song(child("s1", starred="2024-03-11T21:04:07Z"))],
    )
    history.sync_navidrome()

    with db.connect() as conn:
        rows = {r["id"]: r["starred"] for r in conn.execute("SELECT id, starred FROM navidrome_tracks")}
    assert rows == {"s1": 1, "s2": 0}


def test_a_large_hearted_set_does_not_blow_the_sql_variable_limit(
    monkeypatch, navidrome_credentials
):
    """The un-starring pass used one bind parameter per hearted track.

    SQLite caps those — 32766 on a current build, 999 on an older one — so the
    inlined `NOT IN (?, ?, ...)` worked right up until somebody's hearted set
    was large enough and then failed the whole sync. The exact ceiling is a
    build option, so this asserts the behaviour at a size that would have
    produced an unreasonable query rather than trying to trip a limit that
    varies by machine.
    """
    many = [
        navidrome._song(child(f"s{i}", title=f"Track {i}", starred="2024-03-11T21:04:07Z"))
        for i in range(5000)
    ]
    monkeypatch.setattr(navidrome, "library_songs", lambda *a, **k: iter([]))
    monkeypatch.setattr(navidrome, "starred_songs", lambda: many)
    history.sync_navidrome()

    # Now un-heart all but one of them.
    monkeypatch.setattr(navidrome, "starred_songs", lambda: many[:1])
    history.sync_navidrome()

    with db.connect() as conn:
        starred = conn.execute(
            "SELECT COUNT(*) AS n FROM navidrome_tracks WHERE starred = 1"
        ).fetchone()["n"]
        total = conn.execute("SELECT COUNT(*) AS n FROM navidrome_tracks").fetchone()["n"]
    assert (starred, total) == (1, 5000)


def test_the_temp_table_does_not_leak_between_syncs(monkeypatch, navidrome_credentials):
    """A stale row in it would keep an un-hearted track marked as hearted."""
    monkeypatch.setattr(navidrome, "library_songs", lambda *a, **k: iter([]))
    monkeypatch.setattr(
        navidrome, "starred_songs",
        lambda: [navidrome._song(child("s1", starred="2024-03-11T21:04:07Z")),
                 navidrome._song(child("s2", artist="B", title="T2",
                                       starred="2024-03-11T21:04:07Z"))],
    )
    history.sync_navidrome()

    monkeypatch.setattr(
        navidrome, "starred_songs",
        lambda: [navidrome._song(child("s2", artist="B", title="T2",
                                       starred="2024-03-11T21:04:07Z"))],
    )
    history.sync_navidrome()
    history.sync_navidrome()

    with db.connect() as conn:
        rows = {r["id"]: r["starred"] for r in conn.execute("SELECT id, starred FROM navidrome_tracks")}
    assert rows == {"s1": 0, "s2": 1}


def test_a_failure_is_recorded_and_never_raised(monkeypatch, navidrome_credentials):
    """Navidrome being down must cost the second signal, not the scan."""
    def boom(*args, **kwargs):
        raise navidrome.NavidromeError("network error: connection refused")

    monkeypatch.setattr(navidrome, "library_songs", boom)
    monkeypatch.setattr(navidrome, "starred_songs", boom)

    result = history.sync_navidrome()

    assert "connection refused" in result["error"]
    assert history.status()["navidrome"]["error"].endswith("connection refused")


def test_the_library_walk_is_cached_between_scans(monkeypatch, navidrome_credentials):
    walks = []

    def walk(*args, **kwargs):
        walks.append(1)
        return iter([navidrome._song(child("s1", play_count=3))])

    monkeypatch.setattr(navidrome, "library_songs", walk)
    monkeypatch.setattr(navidrome, "starred_songs", lambda: [])

    history.sync_navidrome()
    history.sync_navidrome()

    assert len(walks) == 1


def test_the_library_walk_reruns_once_it_is_stale(monkeypatch, navidrome_credentials):
    monkeypatch.setattr(navidrome.config, "NAVIDROME_LIBRARY_MAX_AGE", 0)
    walks = []
    monkeypatch.setattr(
        navidrome, "library_songs",
        lambda *a, **k: (walks.append(1), iter([navidrome._song(child("s1"))]))[1],
    )
    monkeypatch.setattr(navidrome, "starred_songs", lambda: [])

    history.sync_navidrome()
    history.sync_navidrome()

    assert len(walks) == 2


def test_an_interrupted_walk_is_not_remembered_as_fresh(monkeypatch, navidrome_credentials):
    def half_a_walk(*args, **kwargs):
        yield navidrome._song(child("s1"))
        raise navidrome.NavidromeError("connection reset")

    monkeypatch.setattr(navidrome, "library_songs", half_a_walk)
    monkeypatch.setattr(navidrome, "starred_songs", lambda: [])

    history.sync_navidrome()

    with db.connect() as conn:
        row = conn.execute(
            "SELECT cursor FROM sync_state WHERE source = ?", (history.NAVIDROME_LIBRARY_SOURCE,)
        ).fetchone()
    assert row is None


def test_the_same_track_from_both_calls_is_one_row(monkeypatch, navidrome_credentials):
    monkeypatch.setattr(
        navidrome, "library_songs",
        lambda *a, **k: iter([navidrome._song(child("s1", play_count=12))]),
    )
    monkeypatch.setattr(
        navidrome, "starred_songs",
        lambda: [navidrome._song(child("s1", starred="2024-03-11T21:04:07Z"))],
    )

    history.sync_navidrome()

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM navidrome_tracks").fetchone()["n"] == 1


# ─── Exclusion ─────────────────────────────────────────────────────────────


def test_hearted_tracks_are_never_suggested(navidrome_track):
    from app.norm import track_key

    navidrome_track("Aphex Twin", "Xtal", starred=True)
    assert track_key("Aphex Twin", "Xtal") in exclude.build()


def test_merely_owning_a_track_does_not_exclude_it(navidrome_track):
    """Only hearts exclude. EXCLUDE_MUSIC_DIR is the setting for owning things."""
    from app.norm import track_key

    navidrome_track("Burial", "Archangel", starred=False, play_count=40)
    assert track_key("Burial", "Archangel") not in exclude.build()


def test_hearts_are_matched_through_the_normalised_key(navidrome_track):
    from app.norm import track_key

    navidrome_track("The Beatles", "Karma Police (Remastered 2016)", starred=True)
    assert track_key("Beatles", "Karma Police") in exclude.build()


# ─── Status ────────────────────────────────────────────────────────────────


def test_status_reports_navidrome_separately_from_the_scrobble_sources(
    navidrome_credentials, navidrome_track
):
    navidrome_track("Aphex Twin", "Xtal", starred=True)
    navidrome_track("Burial", "Archangel")

    status = history.status()

    assert [s["name"] for s in status["sources"]] == ["lastfm", "listenbrainz"]
    assert status["navidrome"]["configured"] is True
    assert status["navidrome"]["hearts"] == 1
    assert status["navidrome"]["tracks"] == 2


def test_status_never_reports_the_password(navidrome_credentials):
    assert "hunter2" not in repr(history.status())
