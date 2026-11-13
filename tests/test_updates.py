"""The daily update check (GitHub releases + PyPI), with the network mocked."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.fakes import FakeServices
from tunegrab.config import data_dir
from tunegrab.net import HttpClient
from tunegrab.updates import (
    CHECK_EVERY_S,
    RELEASES_URL,
    YTDLP_URL,
    UpdateChecker,
    is_newer,
    parse_version,
)
from tunegrab.web.app import create_app


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def checker(tmp_path: Path, clock: Clock, ytdlp: str | None = "2026.10.31") -> UpdateChecker:
    return UpdateChecker(
        HttpClient(retries=0),
        tmp_path / "update-check.json",
        current=lambda: "0.4.1",
        current_ytdlp=lambda: ytdlp,
        clock=clock,
    )


def mock_sites(tag: str = "v0.5.0", ytdlp: str = "2026.11.10") -> tuple[Any, Any]:
    github = respx.get(RELEASES_URL).respond(
        200, json={"tag_name": tag, "html_url": f"https://github.com/x/releases/{tag}"}
    )
    pypi = respx.get(YTDLP_URL).respond(200, json={"info": {"version": ytdlp}})
    return github, pypi


def test_versions_compare_as_numbers() -> None:
    assert parse_version("v0.10.0") == (0, 10, 0) and parse_version("nightly") is None
    assert is_newer("0.10.0", "0.9.9") and not is_newer("0.4.1", "0.4.1")
    assert is_newer("2026.11.10", "2026.10.31") and not is_newer(None, "1.0")


@respx.mock
def test_newer_versions_are_reported(tmp_path: Path) -> None:
    mock_sites()
    info = checker(tmp_path, Clock()).check()
    assert info.tunegrab == "0.5.0" and info.tunegrab_url == "https://github.com/x/releases/v0.5.0"
    assert info.ytdlp == "2026.11.10" and info.ytdlp_current == "2026.10.31"


@respx.mock
def test_same_versions_report_nothing(tmp_path: Path) -> None:
    mock_sites(tag="v0.4.1", ytdlp="2026.10.31")
    info = checker(tmp_path, Clock()).check()
    assert info.tunegrab is None and info.tunegrab_url is None and info.ytdlp is None


@respx.mock
def test_at_most_one_check_a_day(tmp_path: Path) -> None:
    github, pypi = mock_sites()
    clock = Clock()
    first = checker(tmp_path, clock)
    first.check()
    clock.now += CHECK_EVERY_S - 60
    again = checker(tmp_path, clock)  # a restart reads the cached answer
    assert not again.due() and again.check().tunegrab == "0.5.0"
    assert github.call_count == 1 and pypi.call_count == 1
    clock.now += 120
    assert again.due()
    again.check()
    assert github.call_count == 2


@respx.mock
def test_offline_is_silent_and_waits_a_day(tmp_path: Path) -> None:
    github = respx.get(RELEASES_URL).mock(side_effect=httpx.ConnectError("offline"))
    respx.get(YTDLP_URL).mock(side_effect=httpx.ConnectError("offline"))
    clock = Clock()
    found = checker(tmp_path, clock)
    info = found.check()
    assert info.tunegrab is None and info.ytdlp is None
    assert not found.due() and github.call_count == 1


@respx.mock
def test_bad_answers_are_ignored(tmp_path: Path) -> None:
    respx.get(RELEASES_URL).respond(404, json={"message": "Not Found"})
    respx.get(YTDLP_URL).respond(200, text="not json")
    info = checker(tmp_path, Clock()).check()
    assert info.tunegrab is None and info.ytdlp is None


@respx.mock
def test_updating_yt_dlp_clears_its_notice(tmp_path: Path) -> None:
    mock_sites()
    installed = {"v": "2026.10.31"}
    found = UpdateChecker(
        HttpClient(retries=0),
        tmp_path / "u.json",
        current=lambda: "0.4.1",
        current_ytdlp=lambda: installed["v"],
        clock=Clock(),
    )
    assert found.check().ytdlp == "2026.11.10"
    installed["v"] = "2026.11.10"  # the Update yt-dlp button ran
    assert found.info().ytdlp is None


def test_a_corrupt_cache_means_check_again(tmp_path: Path) -> None:
    (tmp_path / "update-check.json").write_text("{nope", encoding="utf-8")
    assert checker(tmp_path, Clock()).due()


@respx.mock
def test_disabled_makes_no_call(services: FakeServices) -> None:
    route = respx.get(url__regex=r"https://(api\.github\.com|pypi\.org)/.*").respond(200, json={})
    assert services.settings.check_updates is False  # tests run with it off (conftest)
    with TestClient(create_app(services=services), base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/system").json()["updates"] is None
    assert route.call_count == 0


@respx.mock
def test_system_reports_what_the_daily_check_found(services: FakeServices) -> None:
    github, pypi = mock_sites(tag="v99.0.0")
    services.settings = services.settings.model_copy(update={"check_updates": True})
    with TestClient(create_app(services=services), base_url="http://127.0.0.1:8765") as client:
        client.get("/api/system")  # starts the check in the background
        deadline = time.monotonic() + 10
        while github.call_count == 0 or not (data_dir() / "update-check.json").exists():
            assert time.monotonic() < deadline, "the update check never ran"
            time.sleep(0.05)
        updates = client.get("/api/system").json()["updates"]
        assert updates["tunegrab"] == "99.0.0" and updates["tunegrab_current"]
        client.get("/api/system")
    assert github.call_count == 1 and pypi.call_count == 1  # cached for the day


@pytest.fixture
def services(tmp_path: Path) -> FakeServices:
    from tunegrab.config import Settings

    return FakeServices(Settings(_env_file=None, output_dir=tmp_path / "lib", lyrics=False))
