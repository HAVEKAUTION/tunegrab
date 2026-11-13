"""Shared httpx client: timeouts, retries with backoff, 429 / Retry-After handling.

Every HTTP call TuneGrab makes itself goes through :class:`HttpClient`. (yt-dlp and
ytmusicapi do their own networking internally; that is out of our hands.)
"""

from __future__ import annotations

import email.utils
import logging
import random
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from tunegrab import __version__
from tunegrab.errors import TuneGrabError

log = logging.getLogger(__name__)

USER_AGENT = f"tunegrab/{__version__} (personal music library tool)"
# For Spotify's public pages. NOT a full desktop-Chrome string: for those Spotify
# serves the JavaScript web player, whose HTML lacks the <meta> tags we read.
BROWSER_USER_AGENT = "Mozilla/5.0"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_S = 120.0


class NetworkError(TuneGrabError):
    """A request could not be completed even after retrying."""


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """Parse a ``Retry-After`` header (delta-seconds or HTTP date) into seconds to wait."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    current = time.time() if now is None else now
    return max(0.0, when.timestamp() - current)


class HttpClient:
    """Thread-safe synchronous HTTP client with retries.

    Retries transport errors and 429/5xx responses up to ``retries`` times with
    exponential backoff and jitter; a 429 ``Retry-After`` header is honoured. After
    the last retry the final response is returned (so callers can inspect the
    status), while a final transport error raises :class:`NetworkError`.
    """

    def __init__(
        self,
        *,
        retries: int = 3,
        backoff: float = 0.5,
        timeout: float = 20.0,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.retries = retries
        self.backoff = backoff
        self._sleep = sleep
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, **(headers or {})},
            transport=transport,
        )

    def _backoff_delay(self, attempt: int) -> float:
        base: float = self.backoff * (2**attempt)
        return base + random.uniform(0, base / 2)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        data: Mapping[str, Any] | None = None,
        auth: tuple[str, str] | None = None,
    ) -> httpx.Response:
        attempt = 0
        while True:
            try:
                response = self._client.request(
                    method, url, params=params, headers=headers, data=data, auth=auth
                )
            except httpx.TransportError as exc:
                if attempt >= self.retries:
                    raise NetworkError(f"{method} {url} failed: {exc}") from exc
                delay = self._backoff_delay(attempt)
                log.debug("%s %s: %s; retrying in %.1fs", method, url, exc, delay)
            else:
                if response.status_code not in RETRY_STATUSES or attempt >= self.retries:
                    return response
                delay_hint = (
                    parse_retry_after(response.headers.get("Retry-After"))
                    if response.status_code == 429
                    else None
                )
                delay = self._backoff_delay(attempt) if delay_hint is None else delay_hint
                if delay > MAX_RETRY_AFTER_S:
                    log.warning("%s asked us to wait %.0fs; giving up for now", url, delay)
                    return response
                log.debug(
                    "%s %s -> %s; retrying in %.1fs", method, url, response.status_code, delay
                )
            self._sleep(delay)
            attempt += 1

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        return self.request("GET", url, params=params, headers=headers)

    def post(
        self,
        url: str,
        *,
        data: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        auth: tuple[str, str] | None = None,
    ) -> httpx.Response:
        return self.request("POST", url, data=data, headers=headers, auth=auth)

    def get_bytes(self, url: str) -> bytes:
        """GET a binary resource; raise :class:`NetworkError` on any non-2xx status."""
        response = self.get(url)
        if not response.is_success:
            raise NetworkError(f"GET {url} -> HTTP {response.status_code}")
        return response.content

    def close(self) -> None:
        self._client.close()


_default_client: HttpClient | None = None
_default_lock = threading.Lock()


def get_client() -> HttpClient:
    """Process-wide shared client."""
    global _default_client
    with _default_lock:
        if _default_client is None:
            _default_client = HttpClient()
        return _default_client
