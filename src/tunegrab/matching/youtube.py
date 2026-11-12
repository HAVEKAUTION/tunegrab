"""Plain YouTube search fallback via yt-dlp ``ytsearch``; also single-video lookup."""

from __future__ import annotations

import logging
import re
from typing import Any

from tunegrab.matching.base import SearchError, build_query
from tunegrab.models import Candidate, Track

log = logging.getLogger(__name__)

_CHANNEL_JUNK = re.compile(r"(\s*-\s*topic|vevo|\s+official)$", re.IGNORECASE)


def clean_channel(name: str | None) -> str | None:
    if not name:
        return None
    cleaned = _CHANNEL_JUNK.sub("", name.strip()).strip()
    return cleaned or name.strip()


class _QuietLogger:
    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp warning: %s", msg)

    def error(self, msg: str) -> None:
        log.debug("yt-dlp error: %s", msg)


def candidate_from_info(info: dict[str, Any]) -> Candidate | None:
    video_id = info.get("id")
    title = info.get("title")
    if not video_id or not title or info.get("ie_key", "Youtube") not in ("Youtube", None):
        return None
    artists = info.get("artists") or ([info["artist"]] if info.get("artist") else [])
    if not artists:
        channel = clean_channel(info.get("channel") or info.get("uploader"))
        artists = [channel] if channel else []
    return Candidate(
        video_id=str(video_id),
        title=str(info.get("track") or title),
        artists=[str(a) for a in artists],
        duration_s=int(info.get("duration") or 0),
        source="youtube",
        album=info.get("album"),
        thumbnail_url=info.get("thumbnail"),
    )


class YouTubeSearchProvider:
    name = "youtube"

    def __init__(self, limit: int = 5) -> None:
        self.limit = limit

    def _extract(self, target: str, flat: bool) -> dict[str, Any]:
        import yt_dlp

        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "logger": _QuietLogger(),
        }
        if flat:
            options["extract_flat"] = "in_playlist"
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(target, download=False)
        except Exception as exc:
            raise SearchError(f"YouTube lookup failed: {exc}") from exc
        return info if isinstance(info, dict) else {}

    def search_query(self, query: str) -> list[Candidate]:
        info = self._extract(f"ytsearch{self.limit}:{query}", flat=True)
        found = (candidate_from_info(e) for e in info.get("entries") or [] if isinstance(e, dict))
        return [c for c in found if c is not None]

    def search(self, track: Track) -> list[Candidate]:
        return self.search_query(build_query(track) + " audio")

    def video(self, video_id: str) -> Candidate:
        """Full metadata for one video (used by ``tunegrab fix``)."""
        info = self._extract(f"https://www.youtube.com/watch?v={video_id}", flat=False)
        candidate = candidate_from_info({**info, "ie_key": "Youtube"})
        if candidate is None:
            raise SearchError(f"Could not read YouTube video {video_id}")
        return candidate
