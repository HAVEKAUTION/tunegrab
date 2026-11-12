"""Official Web API provider + auth + resolver fallback (respx-mocked)."""

from __future__ import annotations

import base64
import hashlib
import threading
import time
import urllib.request
from typing import Any

import httpx
import pytest
import respx

from tests.fakes import FakeMetadata
from tunegrab.metadata.base import MetadataError, NotFoundError, RestrictedError
from tunegrab.metadata.resolver import Resolver
from tunegrab.metadata.spotify_api import (
    API_BASE,
    TOKEN_URL,
    MemoryTokenStore,
    SpotifyAPIProvider,
    SpotifyAuth,
    build_authorize_url,
    make_pkce_pair,
    track_from_api,
    wait_for_callback,
)
from tunegrab.models import Collection
from tunegrab.net import HttpClient

ID = "4cOdK2wGLETKBW3PvgPWqT"


def api_track(n: int, **extra: Any) -> dict[str, Any]:
    data = {
        "id": f"{n:022d}",
        "name": f"Song {n}",
        "type": "track",
        "artists": [{"name": "A"}, {"name": "B"}],
        "track_number": n,
        "disc_number": 1,
        "duration_ms": 1000 * (180 + n),
        "explicit": n % 2 == 0,
        "external_ids": {"isrc": f"ISRC{n}"},
        "album": {
            "id": "alb",
            "name": "The Album",
            "artists": [{"name": "A"}],
            "release_date": "2001-02-03",
            "total_tracks": 12,
            "images": [
                {"url": "small", "width": 64, "height": 64},
                {"url": "big", "width": 640, "height": 640},
            ],
        },
    }
    data.update(extra)
    return data


@pytest.fixture
def http() -> HttpClient:
    return HttpClient(retries=0)


@pytest.fixture
def provider(http: HttpClient) -> SpotifyAPIProvider:
    return SpotifyAPIProvider(SpotifyAuth("cid", "secret", http, MemoryTokenStore()), http)


def mock_token(expires_in: int = 3600) -> respx.Route:
    return respx.post(TOKEN_URL).respond(
        200, json={"access_token": "tok", "token_type": "Bearer", "expires_in": expires_in}
    )


def test_track_mapping() -> None:
    track = track_from_api(api_track(3))
    assert track is not None
    assert track.artists == ["A", "B"] and track.album == "The Album"
    assert track.album_artist == "A" and track.cover_url == "big"
    assert (track.isrc, track.total_tracks, track.release_date) == ("ISRC3", 12, "2001-02-03")
    assert track_from_api({"is_local": True, "id": None, "name": "x"}) is None
    assert track_from_api({"type": "episode", "id": "x", "name": "y"}) is None
    assert track_from_api({}) is None


@respx.mock
def test_client_credentials_cached(provider: SpotifyAPIProvider) -> None:
    token = mock_token()
    respx.get(f"{API_BASE}/tracks/{ID}").respond(200, json=api_track(1))
    provider.get_track(ID)
    provider.get_track(ID)
    assert token.call_count == 1
    sent = respx.calls.last.request
    assert sent.headers["Authorization"] == "Bearer tok"
    basic = token.calls[0].request.headers["Authorization"]
    assert base64.b64decode(basic.split()[1]) == b"cid:secret"


@respx.mock
def test_token_refetched_when_expired(http: HttpClient) -> None:
    now = [1000.0]
    auth = SpotifyAuth("cid", "secret", http, MemoryTokenStore(), clock=lambda: now[0])
    token = mock_token(expires_in=3600)
    auth.access_token()
    now[0] += 3600
    auth.access_token()
    assert token.call_count == 2


@respx.mock
def test_401_retries_with_new_token(provider: SpotifyAPIProvider) -> None:
    token = mock_token()
    respx.get(f"{API_BASE}/tracks/{ID}").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json=api_track(1))]
    )
    assert provider.get_track(ID).title == "Song 1"
    assert token.call_count == 2


@respx.mock
def test_bad_credentials(provider: SpotifyAPIProvider) -> None:
    respx.post(TOKEN_URL).respond(400, json={"error": "invalid_client"})
    with pytest.raises(RestrictedError, match="client ID"):
        provider.get_track(ID)


@respx.mock
@pytest.mark.parametrize(
    ("status", "error"),
    [(403, RestrictedError), (404, NotFoundError), (429, MetadataError), (500, MetadataError)],
)
def test_error_mapping(provider: SpotifyAPIProvider, status: int, error: type) -> None:
    mock_token()
    respx.get(f"{API_BASE}/playlists/{ID}").respond(status)
    with pytest.raises(error):
        provider.get_playlist(ID)


@respx.mock
def test_playlist_pagination_and_skips(provider: SpotifyAPIProvider) -> None:
    mock_token()
    page2 = f"{API_BASE}/playlists/{ID}/tracks?offset=2"
    respx.get(f"{API_BASE}/playlists/{ID}").respond(
        200,
        json={
            "name": "Mix",
            "owner": {"display_name": "me"},
            "tracks": {
                "total": 5,
                "next": page2,
                "items": [
                    {"track": api_track(1)},
                    {"track": None},
                ],
            },
        },
    )
    respx.get(page2).respond(
        200,
        json={
            "next": None,
            "items": [
                {"track": {**api_track(2), "is_local": True}},
                {"track": {"type": "episode", "id": "e", "name": "pod"}},
                {"track": api_track(3)},
            ],
        },
    )
    playlist = provider.get_playlist(ID)
    assert [t.title for t in playlist.tracks] == ["Song 1", "Song 3"]
    assert (playlist.skipped, playlist.total, playlist.owner) == (3, 5, "me")
    assert "Skipped 3" in playlist.warnings[0]


@respx.mock
def test_playlist_new_items_field(provider: SpotifyAPIProvider) -> None:
    """Feb 2026 API shape: 'items' instead of 'tracks', entries under 'item'."""
    mock_token()
    respx.get(f"{API_BASE}/playlists/{ID}").respond(
        200,
        json={
            "name": "New",
            "items": {"total": 1, "next": None, "items": [{"item": api_track(7)}]},
        },
    )
    assert [t.title for t in provider.get_playlist(ID).tracks] == ["Song 7"]


@respx.mock
def test_playlist_items_endpoint_fallback(provider: SpotifyAPIProvider) -> None:
    mock_token()
    respx.get(f"{API_BASE}/playlists/{ID}").respond(200, json={"name": "Bare"})
    respx.get(f"{API_BASE}/playlists/{ID}/items").respond(404)
    respx.get(f"{API_BASE}/playlists/{ID}/tracks").respond(
        200, json={"next": None, "items": [{"track": api_track(1)}]}
    )
    assert len(provider.get_playlist(ID).tracks) == 1


@respx.mock
def test_album(provider: SpotifyAPIProvider) -> None:
    mock_token()
    album = {
        "id": "alb",
        "name": "LP",
        "artists": [{"name": "A"}],
        "total_tracks": 2,
        "release_date": "1999",
        "images": [],
        "genres": ["rock"],
        "tracks": {
            "next": None,
            "items": [{k: v for k, v in api_track(i).items() if k != "album"} for i in (1, 2)],
        },
    }
    respx.get(f"{API_BASE}/albums/alb").respond(200, json=album)
    result = provider.get_album("alb")
    assert [t.album for t in result.tracks] == ["LP", "LP"]
    assert result.tracks[0].genres == ["rock"] and result.tracks[1].release_date == "1999"


@respx.mock
def test_artist_dedupes_and_prefers_albums(provider: SpotifyAPIProvider) -> None:
    mock_token()
    respx.get(f"{API_BASE}/artists/art").respond(200, json={"name": "A", "genres": ["pop"]})
    respx.get(f"{API_BASE}/artists/art/albums").respond(
        200,
        json={
            "next": None,
            "items": [
                {"id": "single", "album_type": "single", "release_date": "2000"},
                {"id": "lp", "album_type": "album", "release_date": "2001"},
            ],
        },
    )

    def album(album_id: str, tracks: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "id": album_id,
            "name": album_id,
            "artists": [{"name": "A"}],
            "tracks": {"next": None, "items": tracks},
        }

    same = {k: v for k, v in api_track(1).items() if k not in ("album", "external_ids")}
    other = {**api_track(9), "artists": [{"name": "Somebody Else"}]}
    respx.get(f"{API_BASE}/albums/single").respond(200, json=album("single", [same]))
    respx.get(f"{API_BASE}/albums/lp").respond(200, json=album("lp", [same, other]))
    tracks = provider.get_artist("art").tracks
    assert [(t.title, t.album) for t in tracks] == [("Song 1", "lp")]
    assert tracks[0].genres == ["pop"]


@respx.mock
def test_liked_requires_login(provider: SpotifyAPIProvider) -> None:
    with pytest.raises(RestrictedError, match="login"):
        provider.get_liked()


@respx.mock
def test_user_token_refresh_and_liked(http: HttpClient) -> None:
    store = MemoryTokenStore({"access_token": "old", "refresh_token": "r1", "expires_at": 0})
    auth = SpotifyAuth("cid", None, http, store)
    refresh = respx.post(TOKEN_URL).respond(200, json={"access_token": "new", "expires_in": 60})
    respx.get(f"{API_BASE}/me/tracks").respond(
        200, json={"next": None, "items": [{"track": api_track(1)}]}
    )
    liked = SpotifyAPIProvider(auth, http).get_liked()
    assert len(liked.tracks) == 1
    assert b"refresh_token=r1" in refresh.calls[0].request.content
    assert store.token and store.token["access_token"] == "new"
    assert store.token["refresh_token"] == "r1"  # kept when not rotated


def test_pkce_pair() -> None:
    verifier, challenge = make_pkce_pair()
    assert 43 <= len(verifier) <= 128
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert challenge == digest.decode()
    url = build_authorize_url("cid", "http://127.0.0.1:8888/callback", challenge, "st")
    assert "code_challenge_method=S256" in url and "state=st" in url and "client_id=cid" in url


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.parametrize(
    ("query", "ok"),
    [
        ("code=abc&state=S", True),
        ("code=abc&state=BAD", False),
        ("error=access_denied&state=S", False),
    ],
)
def test_wait_for_callback(query: str, ok: bool) -> None:
    port = _free_port()
    redirect = f"http://127.0.0.1:{port}/callback"
    result: dict[str, Any] = {}

    def serve() -> None:
        try:
            result["code"] = wait_for_callback(redirect, "S", timeout=10)
        except MetadataError as exc:
            result["error"] = str(exc)

    thread = threading.Thread(target=serve)
    thread.start()
    for _ in range(50):
        try:
            urllib.request.urlopen(f"{redirect}?{query}", timeout=2)
            break
        except urllib.error.HTTPError:
            break  # 400 for failures: server answered
        except OSError:
            time.sleep(0.05)
    thread.join(10)
    assert (result.get("code") == "abc") is ok
    if not ok:
        assert "error" in result


def test_from_settings() -> None:
    from tunegrab.config import Settings

    http = HttpClient()
    assert SpotifyAPIProvider.from_settings(Settings(_env_file=None), http) is None
    settings = Settings(_env_file=None, SPOTIFY_CLIENT_ID="id", SPOTIFY_CLIENT_SECRET="s")
    assert SpotifyAPIProvider.from_settings(settings, http) is not None


# --- resolver --------------------------------------------------------------------


class Broken(FakeMetadata):
    name = "broken"

    def get_playlist(self, playlist_id: str) -> Collection:
        raise RestrictedError("dev mode says no")


def test_resolver_falls_back() -> None:
    resolver = Resolver([Broken(), FakeMetadata()], HttpClient())
    collection = resolver.resolve(f"https://open.spotify.com/playlist/{ID}")
    assert collection.source == "fake"
    assert "dev mode says no" in collection.warnings[0]


def test_resolver_all_fail() -> None:
    resolver = Resolver([Broken()], HttpClient())
    with pytest.raises(MetadataError, match="dev mode"):
        resolver.resolve(f"spotify:playlist:{ID}")


def test_resolver_single_track() -> None:
    meta = FakeMetadata()
    track_id = meta.tracks[0].spotify_id
    collection = Resolver([meta], HttpClient()).resolve(f"spotify:track:{track_id}")
    assert collection.kind == "track" and len(collection.tracks) == 1


@respx.mock
def test_resolver_short_link() -> None:
    respx.get("https://spotify.link/abc").respond(
        200, text=f'<a href="https://open.spotify.com/album/{ID}?si=x">open</a>'
    )
    assert (
        Resolver([FakeMetadata()], HttpClient(retries=0)).parse("https://spotify.link/abc").id == ID
    )


def test_saved_login_from_another_client_id_is_ignored(http: HttpClient) -> None:
    far = time.time() + 3600
    other = MemoryTokenStore({"access_token": "u", "expires_at": far, "client_id": "old-app"})
    assert SpotifyAuth("cid", "secret", http, other).has_user is False
    same = MemoryTokenStore({"access_token": "u", "expires_at": far, "client_id": "cid"})
    assert SpotifyAuth("cid", "secret", http, same).access_token() == "u"
    legacy = MemoryTokenStore({"access_token": "u", "expires_at": far})  # saved before 0.3
    assert SpotifyAuth("cid", "secret", http, legacy).has_user is True
