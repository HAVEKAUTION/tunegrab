"""Embed provider against real saved Spotify pages (tests/fixtures/spotify)."""

import pytest
import respx

from tests.conftest import fixture_text
from tunegrab.metadata.base import MetadataError, NotFoundError, RestrictedError
from tunegrab.metadata.spotify_embed import (
    SpotifyEmbedProvider,
    album_from_description,
    parse_entity,
    parse_meta,
    split_artists,
)
from tunegrab.models import Track
from tunegrab.net import HttpClient

TRACK = "4cOdK2wGLETKBW3PvgPWqT"
TRACK_ALBUM = "5Z9iiGl2FcIfa3BMiv6OIw"
ALBUM = "4m2880jivSbbyEGAKfITCa"
PLAYLIST = "37i9dQZF1DXcBWIGoYBM5M"
BIG = "2YRe7HRKNRvXdJBp9nXFza"
ARTIST = "4tZwfgrHOc3mvqYlEYSvVi"
BASE = "https://open.spotify.com"


def fx(name: str) -> str:
    return fixture_text("spotify", name)


@pytest.fixture
def provider() -> SpotifyEmbedProvider:
    return SpotifyEmbedProvider(HttpClient(retries=0))


def mock_track_pages() -> None:
    respx.get(f"{BASE}/embed/track/{TRACK}").respond(200, text=fx("embed_track.html"))
    respx.get(f"{BASE}/track/{TRACK}").respond(200, text=fx("page_track.html"))
    respx.get(f"{BASE}/embed/album/{TRACK_ALBUM}").respond(200, text=fx("embed_album_rick.html"))
    respx.get(f"{BASE}/album/{TRACK_ALBUM}").respond(200, text=fx("page_album.html"))


def test_helpers() -> None:
    assert split_artists("KAROL G,\xa0Judeline,\xa0rusowsky") == ["KAROL G", "Judeline", "rusowsky"]
    assert split_artists("Tyler, The Creator") == ["Tyler, The Creator"]
    assert split_artists(None) == []
    assert album_from_description("Rick Astley · Whenever You Need Somebody · Song · 1987") == (
        "Whenever You Need Somebody"
    )
    assert album_from_description("A · B · C · Song · 2000") == "B · C"
    assert album_from_description("just text") is None
    meta = parse_meta('<meta name="a" content="1 &amp; 2"/><meta content="x" property="b">')
    assert meta == {"a": ["1 & 2"], "b": ["x"]}


def test_parse_entity_errors() -> None:
    with pytest.raises(NotFoundError):
        parse_entity(fx("embed_404.html"))
    with pytest.raises(MetadataError):
        parse_entity("<html>no data</html>")


@respx.mock
def test_get_track_with_album_details(provider: SpotifyEmbedProvider) -> None:
    mock_track_pages()
    track = provider.get_track(TRACK)
    assert track.title == "Never Gonna Give You Up"
    assert track.artists == ["Rick Astley"]
    assert track.duration_ms == 213573
    assert track.album == "Whenever You Need Somebody"
    assert track.album_id == TRACK_ALBUM
    assert track.album_artist == "Rick Astley"
    assert track.track_number == 1
    assert track.total_tracks and track.total_tracks >= 10
    assert track.release_date == "1987-11-12"
    assert track.cover_url and "spotifycdn" in track.cover_url
    assert not track.is_incomplete


@respx.mock
def test_track_page_failure_still_returns_basics(provider: SpotifyEmbedProvider) -> None:
    respx.get(f"{BASE}/embed/track/{TRACK}").respond(200, text=fx("embed_track.html"))
    respx.get(f"{BASE}/track/{TRACK}").respond(500)
    track = provider.get_track(TRACK)
    assert track.title == "Never Gonna Give You Up" and track.album is None
    assert track.album_artist == "Rick Astley"


@respx.mock
def test_get_album(provider: SpotifyEmbedProvider) -> None:
    respx.get(f"{BASE}/embed/album/{ALBUM}").respond(200, text=fx("embed_album.html"))
    respx.get(f"{BASE}/album/{ALBUM}").respond(
        200, text='<meta name="music:release_date" content="2013-05-20"/>'
    )
    album = provider.get_album(ALBUM)
    assert album.kind == "album" and album.name == "Random Access Memories"
    assert len(album.tracks) == 13 and album.total == 13
    first = album.tracks[0]
    assert first.title == "Give Life Back to Music"
    assert (first.track_number, first.album, first.album_artist) == (
        1,
        "Random Access Memories",
        "Daft Punk",
    )
    assert first.release_date == "2013-05-20"
    assert album.tracks[-1].track_number == 13
    assert all(t.cover_url == first.cover_url for t in album.tracks)


@respx.mock
def test_get_playlist(provider: SpotifyEmbedProvider) -> None:
    respx.get(f"{BASE}/embed/playlist/{PLAYLIST}").respond(200, text=fx("embed_playlist.html"))
    playlist = provider.get_playlist(PLAYLIST)
    assert playlist.kind == "playlist" and len(playlist.tracks) == 50
    assert playlist.warnings == []
    multi = [t for t in playlist.tracks if len(t.artists) > 1]
    assert multi and all("\xa0" not in a for t in multi for a in t.artists)
    assert all(t.album is None for t in playlist.tracks)  # filled in later by enrich()


@respx.mock
def test_big_playlist_warns_about_cap(provider: SpotifyEmbedProvider) -> None:
    respx.get(f"{BASE}/embed/playlist/{BIG}").respond(200, text=fx("embed_playlist_big.html"))
    respx.get(f"{BASE}/playlist/{BIG}").respond(200, text=fx("page_playlist_big.html"))
    playlist = provider.get_playlist(BIG)
    assert len(playlist.tracks) == 100
    assert playlist.total == 1223
    assert "first 100" in playlist.warnings[0] and "1223" in playlist.warnings[0]


@respx.mock
def test_get_artist(provider: SpotifyEmbedProvider) -> None:
    respx.get(f"{BASE}/embed/artist/{ARTIST}").respond(200, text=fx("embed_artist.html"))
    artist = provider.get_artist(ARTIST)
    assert artist.name == "Daft Punk" and len(artist.tracks) == 10
    assert "top tracks" in artist.warnings[0]


def test_liked_is_restricted(provider: SpotifyEmbedProvider) -> None:
    with pytest.raises(RestrictedError):
        provider.get_liked()


@respx.mock
def test_enrich(provider: SpotifyEmbedProvider) -> None:
    mock_track_pages()
    bare = Track(
        spotify_id=TRACK, title="Never Gonna Give You Up", artists=["x"], duration_ms=213573
    )
    full = provider.enrich(bare)
    assert full.artists == ["Rick Astley"]
    assert full.album == "Whenever You Need Somebody" and full.release_date == "1987-11-12"
    assert provider.enrich(full) is full  # complete -> no requests


@respx.mock
def test_http_404(provider: SpotifyEmbedProvider) -> None:
    respx.get(f"{BASE}/embed/track/{TRACK}").respond(404)
    with pytest.raises(NotFoundError):
        provider.get_track(TRACK)
