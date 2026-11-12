"""Is a newer TuneGrab (GitHub releases) or yt-dlp (PyPI) out? Checked at most once a day.

The answer is cached in the data folder, so restarts don't ask again. Being offline, or
either site failing, is silent: the check just tries again the next day. Turned off with
``check_updates = false`` (Settings: Updates).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any

from tunegrab import __version__
from tunegrab.net import HttpClient, NetworkError

log = logging.getLogger(__name__)

RELEASES_URL = "https://api.github.com/repos/HAVEKAUTION/tunegrab/releases/latest"
YTDLP_URL = "https://pypi.org/pypi/yt-dlp/json"
CHECK_EVERY_S = 24 * 3600


def parse_version(text: str | None) -> tuple[int, ...] | None:
    """``v0.4.1`` -> (0, 4, 1); ``2026.10.31`` -> (2026, 10, 31); anything else -> None."""
    if not text:
        return None
    found = re.match(r"v?(\d+(?:\.\d+)*)", text.strip())
    return tuple(int(part) for part in found.group(1).split(".")) if found else None


def is_newer(latest: str | None, current: str | None) -> bool:
    new, old = parse_version(latest), parse_version(current)
    return new is not None and old is not None and new > old


def installed_ytdlp() -> str | None:
    try:
        return package_version("yt-dlp")
    except PackageNotFoundError:
        return None


@dataclass(frozen=True)
class UpdateInfo:
    tunegrab: str | None = None  # a newer TuneGrab, if any
    tunegrab_url: str | None = None  # its release notes
    ytdlp: str | None = None  # a newer yt-dlp, if any
    ytdlp_current: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "tunegrab": self.tunegrab,
            "tunegrab_url": self.tunegrab_url,
            "tunegrab_current": __version__,
            "ytdlp": self.ytdlp,
            "ytdlp_current": self.ytdlp_current,
        }


class UpdateChecker:
    def __init__(
        self,
        http: HttpClient,
        cache_path: Path,
        *,
        current: Callable[[], str] = lambda: __version__,
        current_ytdlp: Callable[[], str | None] = installed_ytdlp,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.http = http
        self.cache_path = cache_path
        self.current = current
        self.current_ytdlp = current_ytdlp
        self.clock = clock
        self._running = threading.Lock()

    # -- cache
    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, Any]) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(data), encoding="utf-8")
        except OSError as exc:  # the check still worked; it'll just run again next start
            log.warning("could not save the update check: %s", exc)

    def due(self) -> bool:
        checked = self._load().get("checked_at")
        return not isinstance(checked, int | float) or self.clock() - checked >= CHECK_EVERY_S

    # -- the check
    def _latest_tunegrab(self) -> tuple[str | None, str | None]:
        response = self.http.get(RELEASES_URL, headers={"Accept": "application/vnd.github+json"})
        if not response.is_success:
            log.info("update check: GitHub answered HTTP %s", response.status_code)
            return None, None
        data = response.json()
        tag = data.get("tag_name") if isinstance(data, dict) else None
        url = data.get("html_url") if isinstance(data, dict) else None
        return (str(tag).lstrip("v") if tag else None), (str(url) if url else None)

    def _latest_ytdlp(self) -> str | None:
        response = self.http.get(YTDLP_URL)
        if not response.is_success:
            log.info("update check: PyPI answered HTTP %s", response.status_code)
            return None
        info = response.json().get("info") or {}
        found = info.get("version") if isinstance(info, dict) else None
        return str(found) if found else None

    def check(self) -> UpdateInfo:
        """Ask GitHub and PyPI if a day has passed since the last time; never raises."""
        if not self.due():
            return self.info()
        data: dict[str, Any] = {"checked_at": self.clock()}
        try:
            data["tunegrab"], data["tunegrab_url"] = self._latest_tunegrab()
        except (NetworkError, ValueError) as exc:
            log.info("update check for TuneGrab failed: %s", exc)
        try:
            data["ytdlp"] = self._latest_ytdlp()
        except (NetworkError, ValueError, AttributeError) as exc:
            log.info("update check for yt-dlp failed: %s", exc)
        self._save(data)
        return self.info(data)

    def check_in_background(self) -> None:
        """Start a check in a thread if one is due and none is running."""
        if not self.due() or not self._running.acquire(blocking=False):
            return

        def run() -> None:
            try:
                self.check()
            except Exception:  # a background nicety must never take anything down
                log.exception("update check failed")
            finally:
                self._running.release()

        threading.Thread(target=run, name="tunegrab-update-check", daemon=True).start()

    def info(self, data: dict[str, Any] | None = None) -> UpdateInfo:
        """What the last check found, compared with what is installed now."""
        data = self._load() if data is None else data
        latest, ytdlp = data.get("tunegrab"), data.get("ytdlp")
        current_ytdlp = self.current_ytdlp()
        newer = is_newer(latest, self.current())
        return UpdateInfo(
            tunegrab=latest if newer else None,
            tunegrab_url=data.get("tunegrab_url") if newer else None,
            ytdlp=ytdlp if is_newer(ytdlp, current_ytdlp) else None,
            ytdlp_current=current_ytdlp,
        )
