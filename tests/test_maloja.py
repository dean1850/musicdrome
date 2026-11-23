"""Reading history from Maloja, and storing what it says.

Contract tests with the transport stubbed. The response shapes are the ones a
real Maloja 3.2.3 serves (and, for the flat ones, what 2.x served). The things
worth pinning down are the ones that fail quietly:

* the scrobbles endpoint takes calendar dates, not timestamps, so the walk has
  to run newest-first and stop at the cursor — and look a little past it, or a
  play that arrived late is never read;
* Maloja returns a track's artists in database order, so the featured artist
  can come back first and take the play's artist key with them;
* Maloja splits "Simon & Garfunkel" in two, and keeping only the first name
  would put "Simon" in your profile;
* a login in MALOJA_URL must never reach an error message.
"""

from __future__ import annotations

import httpx
import pytest

from app import config, db, history
from app.norm import artist_key, track_key
from app.sources import maloja

NOW = 1_800_000_000


def scrobble(
    time: int,
    artists: list[str] | str = "Radiohead",
    title: str = "Karma Police",
    album: str | None = "OK Computer",
    album_artists: list[str] | None = None,
) -> dict:
    """One scrobble as Maloja 3.x serialises it."""
    artists = [artists] if isinstance(artists, str) else artists
    return {
        "time": time,
        "track": {
            "artists": artists,
            "title": title,
            "album": (
                {"artists": album_artists or artists[:1], "albumtitle": album}
                if album is not None else None
            ),
            "length": None,
        },
        "duration": None,
        "origin": "client:default",
    }


def page(*entries: dict) -> dict:
    return {"status": "ok", "list": list(entries), "pagination": {}}


@pytest.fixture
def maloja_url(monkeypatch):
    monkeypatch.setattr(config, "MALOJA_URL", "http://maloja.test:42010")
    return "http://maloja.test:42010"


def stub_transport(monkeypatch, *responses):
    """Answer requests in order with the given bodies; return what was sent.

    The last response repeats, so a test only spells out as many pages as it
    cares about.
    """
    sent: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        body = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body)

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(maloja.httpx, "Client", fake_client)
    return sent


def stub_pages(monkeypatch, pages: list[list[dict]]):
    """Serve ``pages`` by the ``page`` parameter; return the pages asked for."""
    asked: list[dict] = []

    def fake_get(endpoint, params=None):
        asked.append(dict(params or {}))
        index = int(params["page"])
        return page(*pages[index]) if index < len(pages) else page()

    monkeypatch.setattr(maloja, "_get", fake_get)
    return asked


# ─── Configuration ─────────────────────────────────────────────────────────


def test_not_configured_without_a_url(monkeypatch):
    monkeypatch.setattr(config, "MALOJA_URL", "")
    assert maloja.configured() is False
    assert "maloja" not in config.history_sources()


def test_the_address_alone_configures_it(maloja_url):
    """No key, no user: the server is the listener."""
    assert maloja.configured() is True
    assert "maloja" in config.history_sources()


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("http://maloja:42010", "http://maloja:42010"),
        ("http://maloja:42010/", "http://maloja:42010"),
        # What the API explorer shows, pasted whole.
        ("http://maloja:42010/apis/mlj_1", "http://maloja:42010"),
        ("http://maloja:42010/apis/mlj_1/", "http://maloja:42010"),
        ("https://example.com/maloja/apis/mlj_1", "https://example.com/maloja"),
        # A compose service name, the way people write it.
        ("maloja:42010", "http://maloja:42010"),
        ("  https://scrobbles.example.com  ", "https://scrobbles.example.com"),
    ],
)
def test_the_url_is_accepted_however_it_was_written(monkeypatch, written, expected):
    monkeypatch.setattr(config, "MALOJA_URL", written)
    assert maloja.base_url() == expected


def test_requests_go_to_the_native_api_and_carry_no_key(monkeypatch, maloja_url):
    sent = stub_transport(monkeypatch, page())

    list(maloja.recent_tracks())

    url = sent[0].url
    assert str(url).startswith("http://maloja.test:42010/apis/mlj_1/scrobbles")
    assert "key" not in url.params and "apikey" not in url.params


def test_get_refuses_when_not_configured(monkeypatch):
    monkeypatch.setattr(config, "MALOJA_URL", "")
    with pytest.raises(maloja.MalojaError, match="MALOJA_URL"):
        maloja.server_info()


# ─── The walk ──────────────────────────────────────────────────────────────


def test_pages_from_zero_and_never_sends_a_time_range(monkeypatch, maloja_url):
    """`from` takes calendar dates; a unix timestamp would be read as a year."""
    full = [scrobble(NOW - i) for i in range(maloja.PAGE_SIZE)]
    asked = stub_pages(monkeypatch, [full, [scrobble(NOW - 10_000)]])

    plays = list(maloja.recent_tracks(since=0))

    assert [a["page"] for a in asked] == [0, 1]
    for params in asked:
        assert params["perpage"] == maloja.PAGE_SIZE
        assert not {"from", "since", "start", "to", "until", "end", "in"} & set(params)
    assert len(plays) == maloja.PAGE_SIZE + 1


def test_a_short_page_is_the_end(monkeypatch, maloja_url):
    asked = stub_pages(monkeypatch, [[scrobble(NOW), scrobble(NOW - 60)]])

    assert len(list(maloja.recent_tracks())) == 2
    assert len(asked) == 1


def test_an_empty_page_after_a_full_one_is_the_end(monkeypatch, maloja_url):
    """Maloja offers a next page whenever the last was full, even at the end."""
    full = [scrobble(NOW - i) for i in range(maloja.PAGE_SIZE)]
    asked = stub_pages(monkeypatch, [full])

    assert len(list(maloja.recent_tracks())) == maloja.PAGE_SIZE
    assert [a["page"] for a in asked] == [0, 1]


def test_a_first_sync_is_capped(monkeypatch, maloja_url):
    full = [scrobble(NOW - i) for i in range(maloja.PAGE_SIZE)]
    asked = stub_pages(monkeypatch, [full] * 10)

    list(maloja.recent_tracks(max_pages=3))
    assert len(asked) == 3


def test_stops_a_day_behind_the_cursor(monkeypatch, maloja_url):
    """Plays already read are re-read for a day, and no further."""
    cursor = NOW
    entries = [
        scrobble(cursor + 600, title="New"),
        scrobble(cursor, title="The cursor itself"),
        scrobble(cursor - 3600, title="Arrived late"),
        scrobble(cursor - maloja.LOOKBACK, title="Exactly a day behind"),
        scrobble(cursor - maloja.LOOKBACK - 1, title="Older"),
    ]
    stub_pages(monkeypatch, [entries])

    titles = [p["title"] for p in maloja.recent_tracks(since=cursor)]
    assert titles == ["New", "The cursor itself", "Arrived late"]


def test_the_walk_stops_at_the_floor_without_reading_further_pages(monkeypatch, maloja_url):
    full = [scrobble(NOW - i) for i in range(maloja.PAGE_SIZE - 1)] + [scrobble(1_000)]
    asked = stub_pages(monkeypatch, [full, [scrobble(500)]])

    list(maloja.recent_tracks(since=NOW))
    assert len(asked) == 1


def test_entries_it_cannot_use_are_skipped_rather_than_ending_the_walk(monkeypatch, maloja_url):
    entries = [
        "not a scrobble",
        {"time": 0, "track": {"artists": ["A"], "title": "No time"}},
        {"time": NOW - 1, "track": {"artists": [], "title": "No artist"}},
        {"time": NOW - 2, "track": {"artists": ["A"], "title": ""}},
        scrobble(NOW - 3, title="Fine"),
    ]
    stub_pages(monkeypatch, [entries])

    assert [p["title"] for p in maloja.recent_tracks()] == ["Fine"]


def test_a_play_carries_what_the_plays_table_needs(monkeypatch, maloja_url):
    stub_pages(monkeypatch, [[scrobble(NOW, "Sigur Rós", "Hoppípolla", album="Takk...")]])

    (play,) = maloja.recent_tracks()
    assert play == {
        "artist": "Sigur Rós",
        "title": "Hoppípolla",
        "album": "Takk...",
        "played_at": NOW,
        "source": "maloja",
    }


def test_a_scrobble_with_no_album_has_an_empty_one(monkeypatch, maloja_url):
    stub_pages(monkeypatch, [[scrobble(NOW, album=None)]])
    assert next(maloja.recent_tracks())["album"] == ""


def test_the_flat_2x_shape_is_read_too(monkeypatch, maloja_url):
    legacy = {
        "artists": ["Portishead"], "title": "Glory Box", "time": NOW,
        "album": "Dummy", "duration": 300,
    }
    stub_pages(monkeypatch, [[legacy]])

    (play,) = maloja.recent_tracks()
    assert (play["artist"], play["title"], play["album"]) == ("Portishead", "Glory Box", "Dummy")


# ─── Artist credits ────────────────────────────────────────────────────────


def test_a_guest_on_the_album_artists_record_is_credited_in_the_title():
    """Exactly what Maloja 3.2.3 returned once Pharrell was already known."""
    artist, title = maloja._artist_and_title(
        ["Pharrell Williams", "Daft Punk"], ["Daft Punk"], "Get Lucky"
    )

    assert (artist, title) == ("Daft Punk", "Get Lucky (feat. Pharrell Williams)")
    # The same track key any other source gives the same play.
    assert track_key(artist, title) == track_key("Daft Punk", "Get Lucky")
    assert track_key(artist, title) == track_key("Daft Punk feat. Pharrell Williams", "Get Lucky")


def test_several_guests_read_as_a_list():
    artist, title = maloja._artist_and_title(
        ["Pharrell Williams", "Daft Punk", "Nile Rodgers"], ["Daft Punk"], "Get Lucky"
    )
    assert (artist, title) == ("Daft Punk", "Get Lucky (feat. Pharrell Williams & Nile Rodgers)")
    assert track_key(artist, title) == track_key("Daft Punk", "Get Lucky")


def test_a_title_that_already_names_its_guest_is_left_alone():
    artist, title = maloja._artist_and_title(
        ["Daft Punk", "Pharrell Williams"], ["Daft Punk"], "Get Lucky (feat. Pharrell Williams)"
    )
    assert (artist, title) == ("Daft Punk", "Get Lucky (feat. Pharrell Williams)")


def test_an_album_sent_without_its_artist_names_no_guests():
    """Maloja copies the track's artists onto such an album, so all of them lead."""
    artist, title = maloja._artist_and_title(
        ["Calvin Harris", "Dua Lipa"], ["Calvin Harris", "Dua Lipa"], "One Kiss"
    )
    assert (artist, title) == ("Calvin Harris & Dua Lipa", "One Kiss")


def test_without_an_album_maloja_order_stands():
    assert maloja._artist_and_title(["Calvin Harris", "Rihanna"], [], "T") == (
        "Calvin Harris & Rihanna", "T",
    )


def test_a_compilation_names_no_guests():
    assert maloja._artist_and_title(["Calvin Harris", "Rihanna"], ["Various Artists"], "T") == (
        "Calvin Harris & Rihanna", "T",
    )


@pytest.mark.parametrize(
    ("split", "band"),
    [
        # How Maloja's default delimiters cut these names up.
        (["Simon", "Garfunkel"], "Simon & Garfunkel"),
        (["Earth, Wind", "Fire"], "Earth, Wind & Fire"),
        (["Crosby, Stills, Nash", "Young"], "Crosby, Stills, Nash & Young"),
    ],
)
def test_a_band_maloja_split_in_two_gets_its_name_back(split, band):
    # With no album, and with the album — which Maloja splits the same way.
    for album_artists in ([], split):
        artist, title = maloja._artist_and_title(split, album_artists, "Song")
        assert (artist, title) == (band, "Song")
        assert artist_key(artist) == artist_key(band)


def test_a_split_band_with_a_guest_keeps_both_halves_and_the_guest():
    artist, title = maloja._artist_and_title(
        ["Simon", "Garfunkel", "Paul Desmond"], ["Simon", "Garfunkel"], "Song"
    )
    assert (artist, title) == ("Simon & Garfunkel", "Song (feat. Paul Desmond)")


def test_a_name_listed_twice_is_credited_once(monkeypatch, maloja_url):
    entry = scrobble(NOW, ["Burial", "burial", "Four Tet"], "Moth", album=None)
    stub_pages(monkeypatch, [[entry]])
    assert next(maloja.recent_tracks())["artist"] == "Burial & Four Tet"


# ─── Failures ──────────────────────────────────────────────────────────────


def test_a_404_says_to_check_the_address(monkeypatch, maloja_url):
    stub_transport(monkeypatch, httpx.Response(404, text="Not Found"))
    with pytest.raises(maloja.MalojaError, match="check MALOJA_URL"):
        list(maloja.recent_tracks())


def test_a_refusal_points_at_the_proxy_not_at_a_key(monkeypatch, maloja_url):
    stub_transport(monkeypatch, httpx.Response(403, text="Forbidden"))
    with pytest.raises(maloja.MalojaError, match="needs no key") as raised:
        list(maloja.recent_tracks())
    assert "user:password@" in str(raised.value)  # the example survives redaction


def test_a_login_page_is_not_mistaken_for_an_empty_history(monkeypatch, maloja_url):
    stub_transport(
        monkeypatch,
        httpx.Response(200, text="<html>Sign in</html>", headers={"Content-Type": "text/html"}),
    )
    with pytest.raises(maloja.MalojaError, match="did not return JSON"):
        list(maloja.recent_tracks())


def test_malojas_own_error_is_passed_on_in_its_words(monkeypatch, maloja_url):
    stub_transport(monkeypatch, httpx.Response(503, json={
        "status": "error",
        "error": {
            "type": "server_not_ready", "value": "db_upgrade",
            "desc": "The database is being upgraded. Please try again later.",
        },
    }))
    with pytest.raises(maloja.MalojaError, match="being upgraded"):
        list(maloja.recent_tracks())


def test_a_login_in_the_url_is_sent_as_basic_auth(monkeypatch):
    monkeypatch.setattr(config, "MALOJA_URL", "https://alice:s3cret@maloja.test")
    sent = stub_transport(monkeypatch, page())

    list(maloja.recent_tracks())
    assert sent[0].headers["Authorization"].startswith("Basic ")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, text="Not Found"),
        httpx.Response(403, text="Forbidden"),
        httpx.Response(200, text="<html>Sign in</html>"),
        httpx.Response(500, text="oops"),
    ],
)
def test_a_login_in_the_url_never_reaches_an_error_message(monkeypatch, response):
    monkeypatch.setattr(config, "MALOJA_URL", "https://alice:s3cret@maloja.test")
    stub_transport(monkeypatch, response)

    with pytest.raises(maloja.MalojaError) as raised:
        list(maloja.recent_tracks())
    assert "s3cret" not in str(raised.value)
    assert "alice" not in str(raised.value)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://alice:s3cret@maloja.test/apis", "https://maloja.test/apis"),
        # An @ inside the password does not leave half of it behind.
        ("https://alice:p@ss@maloja.test/apis", "https://maloja.test/apis"),
        ("reaching http://bob:pw@10.0.0.2:42010: refused", "reaching http://10.0.0.2:42010: refused"),
        # Nothing to take out: an @ after the host is not a login.
        ("http://maloja.test/apis/mlj_1/scrobbles?q=a@b", "http://maloja.test/apis/mlj_1/scrobbles?q=a@b"),
    ],
)
def test_redaction_takes_out_the_login_and_only_the_login(text, expected):
    assert maloja._redact(text) == expected


def test_a_network_error_names_the_server_but_not_the_login(monkeypatch):
    monkeypatch.setattr(config, "MALOJA_URL", "https://alice:s3cret@maloja.test")
    monkeypatch.setattr(maloja.net, "BACKOFF", ())  # no sleeping between retries

    def refuse(request):
        raise httpx.ConnectError("[Errno 111] Connection refused", request=request)

    real_client = httpx.Client
    monkeypatch.setattr(
        maloja.httpx, "Client",
        lambda *a, **kw: real_client(*a, **{**kw, "transport": httpx.MockTransport(refuse)}),
    )

    with pytest.raises(maloja.MalojaError, match="network error reaching https://maloja.test") as raised:
        list(maloja.recent_tracks())
    assert "s3cret" not in str(raised.value)


# ─── Into the plays table ──────────────────────────────────────────────────


def test_maloja_alone_is_a_listening_history(maloja_url):
    assert history.configured() is True


def test_a_sync_stores_plays_under_their_own_source(monkeypatch, maloja_url):
    stub_pages(monkeypatch, [[
        scrobble(NOW, "Radiohead", "Karma Police"),
        scrobble(NOW - 60, ["Pharrell Williams", "Daft Punk"], "Get Lucky",
                 album="Random Access Memories", album_artists=["Daft Punk"]),
    ]])

    result = history.sync()

    assert result["sources"]["maloja"] == {"added": 2, "cursor": NOW, "error": ""}
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT artist, title, artist_key, track_key, source FROM plays ORDER BY played_at"
        ).fetchall()
    assert [(r["artist"], r["title"], r["artist_key"], r["source"]) for r in rows] == [
        ("Daft Punk", "Get Lucky (feat. Pharrell Williams)", "daft punk", "maloja"),
        ("Radiohead", "Karma Police", "radiohead", "maloja"),
    ]
    # So a suggestion of Get Lucky is recognised as something you have played.
    assert rows[0]["track_key"] == track_key("Daft Punk", "Get Lucky")


def test_re_reading_the_lookback_adds_nothing_twice(monkeypatch, maloja_url):
    entries = [scrobble(NOW, title="One"), scrobble(NOW - 60, title="Two")]
    stub_pages(monkeypatch, [entries])

    history.sync()
    second = history.sync()

    assert second["sources"]["maloja"]["added"] == 0
    assert second["sources"]["maloja"]["cursor"] == NOW


def test_a_late_play_is_picked_up_on_the_next_sync(monkeypatch, maloja_url):
    first = [scrobble(NOW, title="Read already")]
    late = [scrobble(NOW, title="Read already"), scrobble(NOW - 7200, title="Arrived late")]
    pages = {"now": first}
    monkeypatch.setattr(maloja, "_get", lambda endpoint, params=None: page(
        *(pages["now"] if params["page"] == 0 else [])
    ))

    history.sync()
    pages["now"] = late
    result = history.sync()

    assert result["sources"]["maloja"]["added"] == 1
    with db.connect() as conn:
        titles = {row["title"] for row in conn.execute("SELECT title FROM plays")}
    assert titles == {"Read already", "Arrived late"}


def test_a_failing_maloja_is_reported_not_raised(monkeypatch, maloja_url):
    def down(endpoint, params=None):
        raise maloja.MalojaError("network error reaching http://maloja.test:42010: refused")

    monkeypatch.setattr(maloja, "_get", down)

    result = history.sync()

    assert "refused" in result["sources"]["maloja"]["error"]
    status = {s["name"]: s for s in history.status()["sources"]}
    assert "refused" in status["maloja"]["error"]


def test_status_lists_maloja_with_the_other_scrobble_sources(maloja_url):
    status = {s["name"]: s for s in history.status()["sources"]}
    assert status["maloja"]["configured"] is True
    assert status["maloja"]["synced_at"] is None
