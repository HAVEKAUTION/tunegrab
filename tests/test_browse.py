"""Artist/release browsing: Spotify artist page parsing, YouTube Music catalog, web API."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

from tests.conftest import fixture_text
from tests.fakes import FakeServices, FakeYTMusicClient
from tunegrab.browse import YTMusicCatalog, parse_artist_page
from tunegrab.config import Settings
from tunegrab.matching.ytmusic import YTMusicClient
from tunegrab.metadata.base import MetadataError, NotFoundError
from tunegrab.models import Track
from tunegrab.web.app import create_app

DAFT_PUNK = "4tZwfgrHOc3mvqYlEYSvVi"


def catalog() -> YTMusicCatalog:
    return YTMusicCatalog(YTMusicClient(FakeYTMusicClient()))


def test_parse_artist_page() -> None:
    overview = parse_artist_page(fixture_text("spotify", "page_artist.html"), DAFT_PUNK)
    assert overview.name == "Daft Punk" and not overview.complete and overview.note
    assert overview.image_url and overview.image_url.startswith("https://i.scdn.co/")
    kinds = {r.kind for r in overview.releases}
    assert kinds == {"album", "single", "compilation"}
    names = [r.name for r in overview.releases]
    assert "Random Access Memories" in names and len(names) == len(
        {r.id for r in overview.releases}
    )
    ram = next(r for r in overview.releases if r.name == "Random Access Memories")
    assert ram.year == "2013" and ram.cover_url and len(ram.id) == 22 and ram.source == "spotify"
    assert overview.top_tracks and overview.top_tracks[0].title == "One More Time"
    assert overview.top_tracks[0].artists == ["Daft Punk"]


def test_parse_artist_page_errors() -> None:
    with pytest.raises(MetadataError):
        parse_artist_page("<html>no data</html>", DAFT_PUNK)
    with pytest.raises(NotFoundError):
        parse_artist_page(fixture_text("spotify", "page_artist.html"), "x" * 22)


def test_ytmusic_artist_full_discography() -> None:
    overview = catalog().artist("fake band")
    assert overview.name == "Fake Band" and overview.complete and overview.id == "UCfake"
    assert [(r.name, r.kind, r.year) for r in overview.releases] == [
        ("Debut", "album", "2001"),  # full list replaces the preview row, duplicates dropped
        ("Second", "album", "2005"),
        ("Hit Single", "single", "2021"),
        ("Old EP", "single", "2009"),
    ]
    assert overview.releases[0].cover_url == "https://lh3/d=w1200-h1200"
    with pytest.raises(NotFoundError):
        YTMusicCatalog(YTMusicClient(type("C", (), {"search": lambda *a, **k: []})())).artist("x")


def test_ytmusic_album_tracks_and_matches() -> None:
    collection, matches = catalog().album("MPREb_debut")
    assert (
        collection.name == "Debut" and collection.source == "ytmusic" and collection.kind == "album"
    )
    assert [t.title for t in collection.tracks] == ["One", "Three"]  # unavailable skipped
    three = collection.tracks[1]
    assert (three.track_number, three.total_tracks, three.release_date, three.duration_ms) == (
        3,
        3,
        "2001",
        150_000,
    )
    assert three.artists == ["Fake Band"] and three.album_artist == "Fake Band" and three.explicit
    assert three.cover_url == "https://lh3/c=w1200-h1200"
    assert list(matches) == ["vid_one_001", "vid_three03"]
    assert matches["vid_three03"].candidate is not None
    assert matches["vid_three03"].accepted(70)
    with pytest.raises(NotFoundError):
        catalog().album("MPREb_missing")


def test_ytmusic_keys_differ_per_album() -> None:
    a = Track(title="Song", artists=["X"], album_id="MPREb_a")
    b = Track(title="Song", artists=["X"], album_id="MPREb_b")
    plain = Track(title="Song", artists=["X"])
    assert len({a.key, b.key, plain.key}) == 3 and plain.key == "q:x - song"


# --- web API --------------------------------------------------------------------------
@pytest.fixture
def services(tmp_path: Path) -> FakeServices:
    return FakeServices(Settings(_env_file=None, output_dir=tmp_path / "lib", lyrics=False))


@pytest.fixture
def client(services: FakeServices) -> Any:
    with TestClient(create_app(services=services), base_url="http://127.0.0.1:8765") as test_client:
        yield test_client


def wait_done(client: TestClient, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job: dict[str, Any] = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] in ("finished", "error"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


@respx.mock
def test_browse_spotify_artist(client: TestClient) -> None:
    respx.get(f"https://open.spotify.com/artist/{DAFT_PUNK}").respond(
        200, text=fixture_text("spotify", "page_artist.html")
    )
    data = client.get(
        "/api/browse", params={"url": f"https://open.spotify.com/artist/{DAFT_PUNK}"}
    ).json()
    assert data["type"] == "artist" and data["name"] == "Daft Punk" and data["complete"] is False
    assert data["releases"] and data["top_tracks"]


def test_browse_text_uses_ytmusic(client: TestClient) -> None:
    data = client.get("/api/browse", params={"url": "Fake Band"}).json()
    assert data["type"] == "artist" and data["source"] == "ytmusic" and len(data["releases"]) == 4
    assert client.get("/api/browse/ytmusic", params={"artist": "Fake Band"}).json()["complete"]
    assert client.get("/api/browse", params={"url": " "}).status_code == 400


def test_browse_collection_has_ids(client: TestClient) -> None:
    data = client.get("/api/browse", params={"url": "spotify:album:4m2880jivSbbyEGAKfITCa"}).json()
    assert data["type"] == "collection" and [t["id"] for t in data["tracks"]] == [
        t["spotify_id"] for t in data["tracks"]
    ]


def test_release_endpoint(client: TestClient) -> None:
    yt = client.get("/api/release", params={"source": "ytmusic", "id": "MPREb_debut"}).json()
    assert [t["id"] for t in yt["tracks"]] == ["vid_one_001", "vid_three03"]
    sp = client.get(
        "/api/release", params={"source": "spotify", "id": "4m2880jivSbbyEGAKfITCa"}
    ).json()
    assert len(sp["tracks"]) == 3 and sp["tracks"][0]["id"] == sp["tracks"][0]["spotify_id"]
    assert client.get("/api/release", params={"source": "nope", "id": "x"}).status_code == 400
    assert (
        client.get("/api/release", params={"source": "ytmusic", "id": "MPREb_missing"}).status_code
        == 404
    )


def test_job_from_ytmusic_album_skips_matching(client: TestClient, services: FakeServices) -> None:
    created = client.post(
        "/api/jobs",
        json={"ytm_album": "MPREb_debut", "track_ids": ["vid_three03"], "name": "Debut"},
    ).json()
    job = wait_done(client, created["id"])
    assert job["counts"]["done"] == 1 and job["name"] == "Debut"
    assert services.downloader.calls == 1
    tasks = client.get(f"/api/jobs/{created['id']}").json()["tasks"]
    assert tasks[0]["title"] == "Three" and tasks[0]["match"]["url"].endswith("vid_three03")
    assert "Fake Band/Debut (2001)/03 - Three.mp3" in tasks[0]["path"].replace("\\", "/")


def test_job_with_selected_spotify_tracks(client: TestClient) -> None:
    album = client.get("/api/browse", params={"url": "spotify:album:4m2880jivSbbyEGAKfITCa"}).json()
    pick = [album["tracks"][0]["id"], album["tracks"][2]["id"]]
    created = client.post(
        "/api/jobs", json={"url": "spotify:album:4m2880jivSbbyEGAKfITCa", "track_ids": pick}
    ).json()
    job = client.get(f"/api/jobs/{wait_done(client, created['id'])['id']}").json()
    assert job["counts"]["done"] == 2 and [t["title"] for t in job["tasks"]] == ["Song 1", "Song 3"]
    tracks = client.post(
        "/api/jobs", json={"spotify_tracks": [pick[1], pick[1]], "name": "Picks"}
    ).json()
    done = wait_done(client, tracks["id"])
    assert (
        done["kind"] == "selection" and done["name"] == "Picks" and done["counts"]["skipped"] == 1
    )


def test_job_request_validation(client: TestClient) -> None:
    assert client.post("/api/jobs", json={}).status_code == 400
    both = {"url": "x", "ytm_album": "MPREb_debut"}
    assert client.post("/api/jobs", json=both).status_code == 400
    created = client.post(
        "/api/jobs", json={"ytm_album": "MPREb_debut", "track_ids": ["nope"]}
    ).json()
    job = wait_done(client, created["id"])
    assert job["state"] == "error" and "None of the selected" in job["error"]


def test_text_naming_an_artist_opens_the_artist(client: TestClient) -> None:
    data = client.get("/api/browse", params={"url": "daft punk"}).json()
    assert data["type"] == "artist" and data["id"] == "UCdaft"


def test_album_and_artist_text_lists_the_album(client: TestClient) -> None:
    data = client.get("/api/browse", params={"url": "Discovery Daft Punk"}).json()
    assert data["type"] == "search" and data["query"] == "Discovery Daft Punk"
    [album] = data["releases"]  # the result without a browse ID is dropped
    assert album["name"] == "Discovery" and album["source"] == "ytmusic"
    assert album["artist"] == "Daft Punk" and album["year"] == "2001" and album["kind"] == "album"
    assert data["artist"] == {"id": "UCdaft", "name": "Daft Punk"}  # offered, not opened


def test_song_title_lists_songs_even_when_a_band_has_that_name(client: TestClient) -> None:
    data = client.get("/api/browse", params={"url": "One More Time"}).json()
    assert data["type"] == "search" and data["releases"] == []
    first, cover = data["songs"]  # the result without a video ID is dropped
    assert first["id"] == "FGBhQbmPwH8" and first["artists"] == ["Daft Punk"]
    assert first["album"] == "Discovery" and first["duration_s"] == 320
    assert cover["album"] is None and cover["explicit"] and cover["duration_s"] is None
    assert data["artist"]["name"] == "One More Time"


def test_text_matching_nothing_is_a_plain_404(client: TestClient) -> None:
    reply = client.get("/api/browse", params={"url": "nothing at all"})
    assert reply.status_code == 404 and "Nothing on YouTube Music matches" in reply.json()["detail"]


def test_artist_by_channel_skips_the_name_search() -> None:
    catalog = YTMusicCatalog(YTMusicClient(FakeYTMusicClient()))
    assert catalog.artist("Daft Punk", channel="UCdaft").id == "UCdaft"
    assert catalog.artist("Fake Band").id == "UCfake"


def test_songs_from_search_download_without_matching(
    client: TestClient, services: FakeServices
) -> None:
    songs = client.get("/api/browse", params={"url": "One More Time"}).json()["songs"]
    one = client.post("/api/jobs", json={"ytm_songs": songs[:1]}).json()
    assert one["name"] == "One More Time"
    job = wait_done(client, one["id"])
    assert job["counts"]["done"] == 1
    [task] = job["tasks"]
    assert task["title"] == "One More Time" and task["match"]["url"].endswith("FGBhQbmPwH8")
    assert task["match"]["reason"] == "picked from search" and services.downloader.calls == 1
    two = client.post("/api/jobs", json={"ytm_songs": songs + songs[:1]}).json()
    assert two["name"] == "Selected songs"
    assert wait_done(client, two["id"])["total"] == 2  # duplicates dropped
    both = client.post("/api/jobs", json={"ytm_songs": songs, "url": "x"})
    assert both.status_code == 400


def test_download_now_names_the_song_it_picked(client: TestClient) -> None:
    created = client.post("/api/jobs", json={"url": "One More Time", "format": "m4a"})
    assert created.status_code == 202
    job = created.json()
    assert job["name"] == "One More Time – Daft Punk" and job["total"] == 1  # known at once
    done = wait_done(client, job["id"])
    assert done["counts"]["done"] == 1 and done["tasks"][0]["path"].endswith(".m4a")


def test_download_now_on_an_artist_name_opens_browse(
    client: TestClient, services: FakeServices
) -> None:
    reply = client.post("/api/jobs", json={"url": " Daft Punk "})
    assert reply.status_code == 409
    assert reply.json() == {
        "detail": "“Daft Punk” is an artist. Pick albums or songs.",
        "browse": "Daft Punk",
    }
    assert services.downloader.calls == 0 and client.get("/api/jobs").json() == []


def test_download_now_with_no_result_is_a_404(
    client: TestClient, services: FakeServices, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services, "lookup_query", lambda text: None)
    reply = client.post("/api/jobs", json={"url": "zzzz qqqq"})
    assert reply.status_code == 404 and "zzzz qqqq" in reply.json()["detail"]


def test_download_now_tries_the_song_when_the_artist_check_fails(
    client: TestClient, services: FakeServices, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tunegrab.matching.base import SearchError

    def broken(text: str) -> None:
        raise SearchError("YouTube Music search failed: boom")

    monkeypatch.setattr(services.browser.ytmusic, "named_artist", broken)
    assert client.post("/api/jobs", json={"url": "One More Time"}).status_code == 202
