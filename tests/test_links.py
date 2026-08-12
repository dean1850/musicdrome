"""Parsing and resolving pasted links."""

import json

import pytest

from app import links


# ─── YouTube URL parsing ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com/watch?v=dQw4w9WgXcQ",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=RDAMVM123",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ?t=42",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ",
        "youtube.com/watch?v=dQw4w9WgXcQ",
    ],
)
def test_every_youtube_url_shape_yields_the_video_id(url):
    assert links.youtube_video_id(url) == "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=tooshort",
        "https://www.youtube.com/results?search_query=abc",
        "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
        "",
    ],
)
def test_non_youtube_urls_yield_nothing(url):
    assert links.youtube_video_id(url) is None


# ─── Spotify URL parsing ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
        "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT?si=abc123",
        "https://open.spotify.com/intl-pt/track/4cOdK2wGLETKBW3PvgPWqT",
        "spotify:track:4cOdK2wGLETKBW3PvgPWqT",
    ],
)
def test_every_spotify_track_url_shape_yields_the_id(url):
    assert links.spotify_track_id(url) == "4cOdK2wGLETKBW3PvgPWqT"


@pytest.mark.parametrize(
    "url",
    [
        "https://open.spotify.com/album/4cOdK2wGLETKBW3PvgPWqT",
        "https://open.spotify.com/playlist/4cOdK2wGLETKBW3PvgPWqT",
        "https://open.spotify.com/artist/4cOdK2wGLETKBW3PvgPWqT",
        "https://example.com/track/4cOdK2wGLETKBW3PvgPWqT",
    ],
)
def test_non_track_spotify_urls_yield_nothing(url):
    assert links.spotify_track_id(url) is None


# ─── Spotify embed scraping ────────────────────────────────────────────────


def embed_page(payload: dict) -> str:
    return (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(payload)}</script></body></html>"
    )


def stub_spotify(monkeypatch, body: str, status: int = 200):
    class Response:
        status_code = status
        text = body

    class Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            return Response()

    monkeypatch.setattr(links.httpx, "Client", Client)


def test_spotify_metadata_from_the_documented_shape(monkeypatch):
    stub_spotify(monkeypatch, embed_page({
        "props": {"pageProps": {"state": {"data": {"entity": {
            "name": "Glory Box",
            "artists": [{"name": "Portishead"}],
            "album": {"name": "Dummy"},
            "duration": 301_000,
        }}}}}
    }))

    meta = links.spotify_metadata("4cOdK2wGLETKBW3PvgPWqT")
    assert meta["artist"] == "Portishead"
    assert meta["title"] == "Glory Box"
    assert meta["album"] == "Dummy"
    assert meta["duration"] == 301
    assert meta["url"] == ""  # nothing downloadable — must be matched instead


def test_spotify_metadata_survives_a_moved_entity(monkeypatch):
    """Spotify has reshuffled this payload before; the walk is the fallback."""
    stub_spotify(monkeypatch, embed_page({
        "props": {"pageProps": {"somethingNew": {"deeper": {
            "name": "Teardrop", "artists": [{"name": "Massive Attack"}],
        }}}}
    }))

    meta = links.spotify_metadata("4cOdK2wGLETKBW3PvgPWqT")
    assert meta["artist"] == "Massive Attack"
    assert meta["title"] == "Teardrop"


def test_spotify_falls_back_to_the_subtitle_for_the_artist(monkeypatch):
    stub_spotify(monkeypatch, embed_page({
        "props": {"pageProps": {"state": {"data": {"entity": {
            "name": "Roygbiv", "subtitle": "Boards of Canada, Someone Else",
        }}}}}
    }))

    assert links.spotify_metadata("4cOdK2wGLETKBW3PvgPWqT")["artist"] == "Boards of Canada"


def test_a_missing_spotify_track_is_reported_clearly(monkeypatch):
    stub_spotify(monkeypatch, "", status=404)
    with pytest.raises(links.LinkError, match="does not have a track"):
        links.spotify_metadata("4cOdK2wGLETKBW3PvgPWqT")


def test_an_unparseable_embed_page_is_reported_clearly(monkeypatch):
    stub_spotify(monkeypatch, "<html>no next data here</html>")
    with pytest.raises(links.LinkError, match="not in the expected format"):
        links.spotify_metadata("4cOdK2wGLETKBW3PvgPWqT")


# ─── Dispatch ──────────────────────────────────────────────────────────────


def test_resolve_routes_a_spotify_link_to_the_embed_reader(monkeypatch):
    monkeypatch.setattr(links, "spotify_metadata", lambda tid: {"artist": "A", "title": "B"})
    assert links.resolve("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT")["artist"] == "A"


def test_resolve_routes_a_youtube_link_to_ytdlp(monkeypatch):
    monkeypatch.setattr(links, "youtube_metadata", lambda vid: {"artist": "A", "title": vid})
    assert links.resolve("https://youtu.be/dQw4w9WgXcQ")["title"] == "dQw4w9WgXcQ"


def test_a_spotify_album_link_says_what_is_wrong_with_it():
    with pytest.raises(links.LinkError, match="paste a track link"):
        links.resolve("https://open.spotify.com/album/4cOdK2wGLETKBW3PvgPWqT")


@pytest.mark.parametrize("url", ["", "   ", "not a url", "https://tidal.com/track/123"])
def test_unsupported_links_are_rejected(url):
    with pytest.raises(links.LinkError):
        links.resolve(url)
