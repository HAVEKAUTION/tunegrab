"""LRCLIB lyrics lookup (synced + plain). Free, no key: https://lrclib.net/docs"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tunegrab.models import Track
from tunegrab.net import HttpClient, NetworkError

log = logging.getLogger(__name__)

LRCLIB_API = "https://lrclib.net/api"
DURATION_TOLERANCE_S = 5
_TIMESTAMP = re.compile(r"^\s*(\[\d+:\d+(?:[.:]\d+)?\])+\s?")


@dataclass(frozen=True)
class Lyrics:
    plain: str | None
    synced: str | None
    instrumental: bool = False

    @property
    def text(self) -> str | None:
        """Best plain text for embedding (synced lyrics with timestamps stripped)."""
        # Some user-submitted "plain" lyrics still carry [mm:ss.xx] stamps; strip them.
        for source in (self.plain, self.synced):
            if source and (text := strip_timestamps(source)):
                return text
        return None


def strip_timestamps(synced: str) -> str:
    return "\n".join(_TIMESTAMP.sub("", line) for line in synced.splitlines()).strip()


def _from_payload(payload: dict[str, Any]) -> Lyrics | None:
    plain = payload.get("plainLyrics") or None
    synced = payload.get("syncedLyrics") or None
    instrumental = bool(payload.get("instrumental"))
    if not plain and not synced and not instrumental:
        return None
    return Lyrics(plain=plain, synced=synced, instrumental=instrumental)


class LyricsClient:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def _get_json(self, endpoint: str, params: dict[str, Any]) -> Any:
        try:
            response = self.http.get(f"{LRCLIB_API}/{endpoint}", params=params)
        except NetworkError as exc:
            log.info("LRCLIB unreachable: %s", exc)
            return None
        if response.status_code == 404 or not response.is_success:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def fetch(self, track: Track) -> Lyrics | None:
        """Exact lookup by title/artist/album/duration, then a fuzzy search."""
        duration = round(track.duration_s)
        if track.album and duration:
            payload = self._get_json(
                "get",
                {
                    "track_name": track.title,
                    "artist_name": track.primary_artist,
                    "album_name": track.album,
                    "duration": duration,
                },
            )
            if isinstance(payload, dict) and (found := _from_payload(payload)):
                return found
        results = self._get_json(
            "search", {"track_name": track.title, "artist_name": track.primary_artist}
        )
        if not isinstance(results, list):
            return None
        candidates = [r for r in results if isinstance(r, dict)]
        if duration:
            candidates = [
                r
                for r in candidates
                if abs(float(r.get("duration") or 0) - duration) <= DURATION_TOLERANCE_S
            ]
        # Prefer entries that have synced lyrics, then plain ones.
        candidates.sort(key=lambda r: (not r.get("syncedLyrics"), not r.get("plainLyrics")))
        for result in candidates:
            if found := _from_payload(result):
                return found
        return None


def write_lrc(audio_path: Path, synced: str) -> Path:
    path = audio_path.with_suffix(".lrc")
    path.write_text(synced.strip() + "\n", encoding="utf-8")
    return path
