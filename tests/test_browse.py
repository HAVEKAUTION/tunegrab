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
