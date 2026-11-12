"""Web UI API with FastAPI's TestClient and FakeServices."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.fakes import ALBUM_ID, FakeServices
from tunegrab.config import Settings, read_config_file
from tunegrab.web import app as web_app
from tunegrab.web.app import create_app

ALBUM = f"https://open.spotify.com/album/{ALBUM_ID}"


@pytest.fixture
def services(tmp_path: Path) -> FakeServices:
    return FakeServices(Settings(_env_file=None, output_dir=tmp_path / "lib", lyrics=False))


@pytest.fixture
def client(services: FakeServices) -> Any:
    with TestClient(create_app(services=services), base_url="http://127.0.0.1:8765") as test_client:
        yield test_client


def wait_for(client: TestClient, job_id: str, states: tuple[str, ...]) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job: dict[str, Any] = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] in states:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job stuck in {job['state']}")


def test_index_and_health(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200 and "TuneGrab" in page.text and "EventSource" in page.text
    assert client.get("/api/health").json()["ok"] is True


def test_preview(client: TestClient) -> None:
    data = client.get("/api/preview", params={"url": ALBUM}).json()
    assert data["name"] == "Fake Album" and len(data["tracks"]) == 3
    assert client.get("/api/preview", params={"url": "nope"}).status_code == 400
    missing = client.get("/api/preview", params={"url": f"spotify:track:{'7' * 22}"})
    assert missing.status_code == 404


def test_job_lifecycle_and_library(client: TestClient) -> None:
    created = client.post("/api/jobs", json={"url": ALBUM})
    assert created.status_code == 202
    job = wait_for(client, created.json()["id"], ("finished",))
    assert job["counts"]["done"] == 3 and len(job["tasks"]) == 3
    assert all(t["status"] == "done" and t["path"] for t in job["tasks"])
    assert job["tasks"][0]["match"]["score"] == 100
    assert [j["id"] for j in client.get("/api/jobs").json()] == [job["id"]]
    library = client.get("/api/library").json()
    assert len(library["tracks"]) == 3 and all(t["exists"] for t in library["tracks"])
    assert client.get("/api/jobs/missing").status_code == 404
    assert client.post("/api/jobs/" + job["id"] + "/retry").status_code == 409  # nothing failed


def test_bad_job_input(client: TestClient) -> None:
    assert client.post("/api/jobs", json={"url": "  "}).status_code == 400
    created = client.post("/api/jobs", json={"url": "https://open.spotify.com/show/" + "x" * 22})
    job = wait_for(client, created.json()["id"], ("error",))
    assert "Unsupported" in job["error"]


def test_retry_failed(client: TestClient, services: FakeServices) -> None:
    services.settings = services.settings.model_copy(update={"retries": 0})
    services.downloader.fail_times = 3
    created = client.post("/api/jobs", json={"url": ALBUM}).json()
    job = wait_for(client, created["id"], ("finished",))
    assert job["counts"]["failed"] == 3
    retried = client.post(f"/api/jobs/{job['id']}/retry")
    assert retried.status_code == 202
    again = wait_for(client, retried.json()["id"], ("finished",))
    assert again["counts"]["done"] == 3


def test_events_are_published(client: TestClient) -> None:
    manager = client.app.state.manager  # type: ignore[attr-defined]
    queue = manager.subscribe()
    created = client.post("/api/jobs", json={"url": ALBUM}).json()
    wait_for(client, created["id"], ("finished",))
    time.sleep(0.1)
    messages = []
    while not queue.empty():
        messages.append(queue.get_nowait())
    manager.unsubscribe(queue)
    assert any(m.startswith("event: job") for m in messages)
    assert any(m.startswith("event: task") and '"status": "done"' in m for m in messages)
    assert all(m.endswith("\n\n") for m in messages)


def test_cancel_unknown_job(client: TestClient) -> None:
    assert client.post("/api/jobs/nope/cancel").status_code == 404


def test_settings_roundtrip(client: TestClient, services: FakeServices, tmp_path: Path) -> None:
    data = client.get("/api/settings").json()
    assert data["values"]["format"] == "mp3" and data["values"]["lyrics"] is False
    new_dir = tmp_path / "Music Here"
    saved = client.post(
        "/api/settings",
        json={"format": "opus", "threads": "2", "lrc": True, "output_dir": str(new_dir)},
    ).json()
    assert saved["values"]["format"] == "opus" and saved["values"]["threads"] == 2
    assert services.settings.output_dir == new_dir and services.settings.lrc is True
    assert read_config_file()["format"] == "opus"
    assert client.get("/api/library").json()["root"] == str(new_dir)
    # blank resets to default
    client.post("/api/settings", json={"format": ""})
    assert services.settings.format == "mp3" and "format" not in read_config_file()


@pytest.mark.parametrize(
    "bad",
    [
        {"threads": "99"},
        {"format": "wav"},
        {"archive_path": "x"},
        {"track_template": "{nope}.{ext}"},
    ],
)
def test_settings_rejects_bad_values(client: TestClient, bad: dict[str, str]) -> None:
    assert client.post("/api/settings", json=bad).status_code == 400


def test_settings_rejects_unusable_music_folders(
    client: TestClient, services: FakeServices, tmp_path: Path
) -> None:
    before = services.settings.output_dir
    blocker = tmp_path / "a file"
    blocker.write_text("x")
    for folder, reason in ((blocker, "can't save music there"), ("relative/music", "full folder")):
        response = client.post("/api/settings", json={"output_dir": str(folder)})
        assert response.status_code == 400 and reason in response.json()["detail"]
    assert services.settings.output_dir == before
    assert "output_dir" not in read_config_file()


def test_open_only_inside_library(
    client: TestClient, services: FakeServices, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    opened: list[Path] = []
    monkeypatch.setattr(web_app, "open_in_file_manager", opened.append)
    assert client.post("/api/open", json={}).status_code == 200  # creates + opens the root
    assert opened == [(tmp_path / "lib").resolve()]
    assert client.post("/api/open", json={"path": str(tmp_path)}).status_code == 400
    assert (
        client.post("/api/open", json={"path": str(tmp_path / "lib" / "x.mp3")}).status_code == 404
    )


def test_pick_folder(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_app, "pick_folder", lambda initial: "D:/Music")
    assert client.post("/api/pick-folder").json() == {"path": "D:/Music"}


def test_local_only_guard(services: FakeServices) -> None:
    app = create_app(services=services)
    with TestClient(app, base_url="http://evil.example:8765") as foreign:
        assert foreign.get("/api/health").status_code == 403  # DNS-rebinding host
    with TestClient(app, base_url="http://localhost:8765") as local:
        assert local.get("/api/health").status_code == 200
        blocked = local.post(
            "/api/settings", json={"threads": "2"}, headers={"Origin": "https://evil.example"}
        )
        assert blocked.status_code == 403 and services.settings.threads == 4
        same = local.post(
            "/api/settings", json={"threads": "2"}, headers={"Origin": "http://localhost:8765"}
        )
        assert same.status_code == 200 and services.settings.threads == 2
    extra = create_app(services=services, allowed_hosts={"192.168.1.5"})
    with TestClient(extra, base_url="http://192.168.1.5:8765") as lan:
        assert lan.get("/api/health").status_code == 200


def test_system_status(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from tunegrab.doctor import CheckResult, Status

    monkeypatch.setattr(
        web_app,
        "run_all",
        lambda s: [
            CheckResult("ffmpeg", Status.OK, "7.1"),
            CheckResult("Deno", Status.FAIL, "missing", "winget install DenoLand.Deno"),
        ],
    )
    data = client.get("/api/system").json()
    assert data["ok"] is False and data["default_folder"] is True and data["failures"] == 0
    assert data["checks"][1] == {
        "name": "Deno",
        "status": "fail",
        "detail": "missing",
        "hint": "winget install DenoLand.Deno",
    }
    assert set(data["paths"]) == {"library", "archive", "log", "config"}


def test_history_persists_and_clears(services: FakeServices) -> None:
    with TestClient(create_app(services=services), base_url="http://127.0.0.1:8765") as c:
        job = wait_for(c, c.post("/api/jobs", json={"url": ALBUM}).json()["id"], ("finished",))
    with TestClient(create_app(services=services), base_url="http://127.0.0.1:8765") as c:
        assert c.get("/api/jobs").json() == []  # a "restart": no live jobs
        [old] = c.get("/api/history").json()
        assert old["id"] == job["id"] and old["counts"]["done"] == 3 and old["state"] == "finished"
        assert c.delete("/api/jobs/finished").json()["cleared"] == 1
        assert c.get("/api/history").json() == []


def test_retry_failures_across_restart(client: TestClient, services: FakeServices) -> None:
    assert client.post("/api/failures/retry").status_code == 409
    services.settings = services.settings.model_copy(update={"retries": 0})
    services.downloader.fail_times = 3
    wait_for(client, client.post("/api/jobs", json={"url": ALBUM}).json()["id"], ("finished",))
    assert client.get("/api/system").json()["failures"] == 3
    retried = client.post("/api/failures/retry")
    assert retried.status_code == 202 and retried.json()["name"] == "Retry failed tracks"
    assert wait_for(client, retried.json()["id"], ("finished",))["counts"]["done"] == 3
    assert services.archive.open_failures() == []


def test_fix_failed_song_with_youtube_link(client: TestClient, services: FakeServices) -> None:
    services.settings = services.settings.model_copy(update={"retries": 0})
    services.downloader.fail_times = 3
    job = wait_for(
        client, client.post("/api/jobs", json={"url": ALBUM}).json()["id"], ("finished",)
    )
    assert job["counts"]["failed"] == 3
    task = job["tasks"][1]
    url = f"/api/jobs/{job['id']}/tasks/{task['id']}/fix"
    assert client.post(url, json={"youtube_url": "not a link"}).status_code == 400
    missing = f"/api/jobs/{job['id']}/tasks/nope/fix"
    assert client.post(missing, json={"youtube_url": "dQw4w9WgXcQ"}).status_code == 404
    fixed = client.post(url, json={"youtube_url": "https://youtu.be/dQw4w9WgXcQ"})
    assert fixed.status_code == 202
    again = wait_for(client, fixed.json()["id"], ("finished",))
    assert again["counts"]["done"] == 1 and len(again["tasks"]) == 1
    assert "dQw4w9WgXcQ" in again["tasks"][0]["match"]["url"]
    key = next(t for t in services.archive.all_downloads()).key
    cached = services.archive.get_match(key)
    assert (
        cached is not None
        and cached[0].video_id == "dQw4w9WgXcQ"
        and cached[2] == "manual override"
    )
    done_task = again["tasks"][0]
    done_url = f"/api/jobs/{again['id']}/tasks/{done_task['id']}/fix"
    assert client.post(done_url, json={"youtube_url": "dQw4w9WgXcQ"}).status_code == 409


def test_csv_import_job(client: TestClient) -> None:
    text = "Track Name,Artist Name(s),Track Duration (ms)\nSong One,Fake Artist,200000\n"
    created = client.post("/api/jobs", json={"csv_text": text, "name": "My Mix"})
    assert created.status_code == 202
    job = wait_for(client, created.json()["id"], ("finished", "error"))
    assert job["state"] == "finished" and job["name"] == "My Mix" and job["total"] == 1
    bad = client.post("/api/jobs", json={"csv_text": "foo,bar\n1,2\n"})
    assert wait_for(client, bad.json()["id"], ("error",))["error"].startswith("The CSV needs")
    both = client.post("/api/jobs", json={"csv_text": text, "url": ALBUM})
    assert both.status_code == 400


def test_update_ytdlp(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def fake_update() -> tuple[bool, str]:
        calls.append(1)
        return (len(calls) == 1, "Updated yt-dlp v2026.10.26 -> v2026.11.12")

    monkeypatch.setattr(web_app, "update_ytdlp", fake_update)
    ok = client.post("/api/system/update-ytdlp")
    assert ok.status_code == 200 and ok.json()["ok"] is True
    failed = client.post("/api/system/update-ytdlp")
    assert failed.status_code == 500 and "Update failed" in failed.json()["detail"]


def test_done_tasks_report_source_quality(client: TestClient) -> None:
    job = wait_for(
        client, client.post("/api/jobs", json={"url": ALBUM}).json()["id"], ("finished",)
    )
    task = job["tasks"][0]
    assert task["source"] == "Opus 129 kbps" and task["low_quality"] is False


def test_video_link_browse_and_download(
    client: TestClient, services: FakeServices, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tunegrab import video
    from tunegrab.video import VideoEntry, VideoProbe

    probes: list[str] = []
    entry = VideoEntry("dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ", "Song", "Chan", 213)

    def fake_probe(url: str, cookies: str | None = None) -> VideoProbe:
        probes.append(url)
        return VideoProbe(url, "video", "Song", "Chan", "Youtube", [entry], heights=[1080, 720])

    downloaded: list[tuple[str, str]] = []

    def fake_download(self: Any, url: str, dest: Path, progress: Any = None, cancel: Any = None):  # type: ignore[no-untyped-def]
        downloaded.append((url, self.options.mode))
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / f"x.{self.extension}"
        out.write_bytes(b"v")
        return out, "720p H.264 + AAC 130 kbps", False

    monkeypatch.setattr(web_app, "probe", fake_probe)
    monkeypatch.setattr(video.VideoDownloader, "download", fake_download)
    clips: list[tuple[float | None, float | None]] = []
    monkeypatch.setattr(
        video,
        "cut_clip",
        lambda path, options, cancel: clips.append((options.start, options.end)) or path,
    )

    url = "https://youtu.be/dQw4w9WgXcQ"
    data = client.get("/api/browse", params={"url": url}).json()
    assert data["type"] == "video" and data["heights"] == [1080, 720]
    assert data["default_quality"] == "1080" and data["folder"].endswith("Videos")

    bad = client.post("/api/video", json={"url": url, "start": "0:30", "end": "0:10"})
    assert bad.status_code == 400
    assert client.post("/api/video", json={"url": "Daft Punk"}).status_code == 400

    started = client.post("/api/video", json={"url": url, "quality": "720", "end": "0:30"})
    assert started.status_code == 202
    job = wait_for(client, started.json()["id"], ("finished",))
    assert job["kind"] == "video" and job["counts"]["done"] == 1
    assert job["tasks"][0]["source"] == "720p H.264 + AAC 130 kbps"
    assert probes == [url]  # browse + download = one lookup (cached)
    assert clips == [(None, 30.0)]

    # "Download now" on a video link saves its audio into the music library
    now = client.post("/api/jobs", json={"url": url}).json()
    done = wait_for(client, now["id"], ("finished",))
    assert done["counts"]["done"] == 1 and downloaded[-1] == (url, "audio")
    assert str(services.settings.resolved_output_dir) in (done["tasks"][0]["path"] or "")


def test_storage_endpoints(client: TestClient) -> None:
    items = client.get("/api/storage").json()["items"]
    assert {"tmp", "logs", "database", "settings", "ytdlp_cache"} <= {i["key"] for i in items}
    assert client.post("/api/storage/clean", json={"keys": ["music"]}).status_code == 400
    done = client.post("/api/storage/clean", json={"keys": ["tmp", "logs"]}).json()
    assert [r["key"] for r in done["results"]] == ["tmp", "logs"]


def test_unexpected_resolve_error_ends_the_job(
    client: TestClient, services: FakeServices, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any exception while resolving ends the job; it never sits in "resolving"."""
    app_manager = client.app.state.manager  # type: ignore[attr-defined]

    def broken(*_: Any) -> Any:
        raise KeyError("tracks")

    monkeypatch.setattr(app_manager, "_build", broken)
    created = client.post("/api/jobs", json={"url": ALBUM}).json()
    job = wait_for(client, created["id"], ("error",))
    assert "Something unexpected went wrong (KeyError" in job["error"]
    assert "log" in job["error"]
    assert not app_manager.busy()


def test_unexpected_video_probe_error_ends_the_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(url: str, cookies: str | None = None) -> Any:
        raise TypeError("'NoneType' object is not subscriptable")

    monkeypatch.setattr(web_app, "probe", broken)
    created = client.post("/api/video", json={"url": "https://youtu.be/dQw4w9WgXcQ"}).json()
    job = wait_for(client, created["id"], ("error",))
    assert "TypeError" in job["error"]
    assert not client.app.state.manager.busy()  # type: ignore[attr-defined]


def test_crashing_pipeline_leaves_no_task_running(
    client: TestClient, services: FakeServices, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tunegrab.pipeline import Pipeline

    async def broken(self: Pipeline, job: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(Pipeline, "run", broken)
    created = client.post("/api/jobs", json={"url": ALBUM}).json()
    job = wait_for(client, created["id"], ("error",))
    assert "RuntimeError: boom" in job["error"]
    assert {t["status"] for t in job["tasks"]} == {"skipped"}
    assert not client.app.state.manager.busy()  # type: ignore[attr-defined]


def test_unusable_music_folder_ends_the_job_with_a_clear_message(
    client: TestClient, services: FakeServices, tmp_path: Path
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    services.settings = services.settings.model_copy(update={"output_dir": blocker / "music"})
    created = client.post("/api/jobs", json={"url": ALBUM}).json()
    job = wait_for(client, created["id"], ("error",))
    assert job["error"].startswith("Your music folder") and "Settings" in job["error"]
    assert not client.app.state.manager.busy()  # type: ignore[attr-defined]


def test_startup_warnings_are_shown_and_dismissed(client: TestClient) -> None:
    from tunegrab import config

    config.startup_warnings.append("Your settings file couldn't be read.")
    try:
        assert client.get("/api/system").json()["warnings"] == [
            "Your settings file couldn't be read."
        ]
        assert client.delete("/api/system/warnings").status_code == 200
        assert client.get("/api/system").json()["warnings"] == []
    finally:
        config.startup_warnings.clear()
