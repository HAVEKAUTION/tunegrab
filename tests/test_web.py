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
