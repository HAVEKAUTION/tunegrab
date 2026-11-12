import email.utils

import httpx
import pytest
import respx

from tunegrab.net import HttpClient, NetworkError, parse_retry_after

URL = "https://api.example/x"


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client(sleeps: list[float]) -> HttpClient:
    return HttpClient(retries=3, backoff=0.1, sleep=sleeps.append)


def test_parse_retry_after() -> None:
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after(" 2.5 ") == 2.5
    assert parse_retry_after("-3") == 0.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None
    when = email.utils.formatdate(1_000_030, usegmt=True)
    assert parse_retry_after(when, now=1_000_000) == pytest.approx(30, abs=1)


@respx.mock
def test_success_no_retry(client: HttpClient, sleeps: list[float]) -> None:
    respx.get(URL).respond(200, json={"ok": 1})
    assert client.get(URL).json() == {"ok": 1}
    assert sleeps == []


@respx.mock
def test_429_honours_retry_after(client: HttpClient, sleeps: list[float]) -> None:
    route = respx.get(URL)
    route.side_effect = [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)]
    assert client.get(URL).status_code == 200
    assert sleeps == [7.0]


@respx.mock
def test_5xx_backoff_then_gives_last_response(client: HttpClient, sleeps: list[float]) -> None:
    respx.get(URL).respond(503)
    assert client.get(URL).status_code == 503
    assert len(sleeps) == 3
    assert sleeps[0] < sleeps[1] < sleeps[2]  # exponential


@respx.mock
def test_4xx_not_retried(client: HttpClient, sleeps: list[float]) -> None:
    respx.get(URL).respond(404)
    assert client.get(URL).status_code == 404
    assert sleeps == []


@respx.mock
def test_huge_retry_after_returns_immediately(client: HttpClient, sleeps: list[float]) -> None:
    respx.get(URL).respond(429, headers={"Retry-After": "3600"})
    assert client.get(URL).status_code == 429
    assert sleeps == []


@respx.mock
def test_transport_errors_retried_then_raise(client: HttpClient, sleeps: list[float]) -> None:
    respx.get(URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(NetworkError):
        client.get(URL)
    assert len(sleeps) == 3


@respx.mock
def test_transport_error_recovers(client: HttpClient) -> None:
    respx.get(URL).mock(side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, text="ok")])
    assert client.get(URL).text == "ok"


@respx.mock
def test_get_bytes(client: HttpClient) -> None:
    respx.get(URL).respond(200, content=b"\x01\x02")
    assert client.get_bytes(URL) == b"\x01\x02"
    respx.get(URL + "/missing").respond(404)
    with pytest.raises(NetworkError):
        client.get_bytes(URL + "/missing")


@respx.mock
def test_user_agent_sent(client: HttpClient) -> None:
    route = respx.get(URL).respond(200)
    client.get(URL)
    assert route.calls[0].request.headers["User-Agent"].startswith("tunegrab/")
