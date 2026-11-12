"""YouTube Music search via ytmusicapi (songs first, then videos).

ytmusicapi does its own HTTP (requests) internally, outside ``net.py``.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Literal

from tunegrab.matching.base import SearchError, build_query, parse_duration
from tunegrab.models import Candidate, CandidateSource, Track

log = logging.getLogger(__name__)

SearchFilter = Literal["songs", "videos"]
_THUMB_SIZE = re.compile(r"=w\d+-h\d+")


def best_thumbnail(thumbnails: list[dict[str, Any]] | None) -> str | None:
    """Largest thumbnail; googleusercontent URLs are upscaled to 1200px (album art)."""
    if not thumbnails:
        return None
    best = max(thumbnails, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
    url = best.get("url")
    if not url:
        return None
    return _THUMB_SIZE.sub("=w1200-h1200", str(url))


def candidate_from_result(result: dict[str, Any], source: CandidateSource) -> Candidate | None:
    video_id = result.get("videoId")
    title = result.get("title")
    if not video_id or not title:
        return None
    seconds = result.get("duration_seconds")
    if not isinstance(seconds, int):
        seconds = parse_duration(result.get("duration"))
    album = result.get("album")
    return Candidate(
        video_id=str(video_id),
        title=str(title),
        artists=[a["name"] for a in result.get("artists") or [] if a.get("name")],
        duration_s=seconds,
        source=source,
        album=album.get("name") if isinstance(album, dict) else None,
        thumbnail_url=best_thumbnail(result.get("thumbnails")),
    )


class YTMusicClient:
    """Lazily-created, lock-protected ytmusicapi client shared by both providers."""

    def __init__(self, client: Any | None = None) -> None:
        self._client = client
        self._lock = threading.Lock()

    def search(self, query: str, kind: SearchFilter, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            try:
                if self._client is None:
                    from ytmusicapi import YTMusic

                    self._client = YTMusic()
                results = self._client.search(query, filter=kind, limit=limit)
            except Exception as exc:  # ytmusicapi raises many kinds of errors
                raise SearchError(f"YouTube Music search failed: {exc}") from exc
        return [r for r in results or [] if isinstance(r, dict)]

    def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Any other ytmusicapi call (get_artist, get_album, ...), same lock/errors."""
        with self._lock:
            try:
                if self._client is None:
                    from ytmusicapi import YTMusic

                    self._client = YTMusic()
                return getattr(self._client, method)(*args, **kwargs)
            except Exception as exc:
                raise SearchError(f"YouTube Music {method} failed: {exc}") from exc


class YTMusicProvider:
    """One ytmusicapi filter (``songs`` or ``videos``) as an AudioProvider."""

    def __init__(self, client: YTMusicClient, kind: SearchFilter = "songs", limit: int = 10):
        self.client = client
        self.kind = kind
        self.limit = limit
        self.name = f"ytmusic_{kind[:-1]}"

    @property
    def source(self) -> CandidateSource:
        return "ytmusic_song" if self.kind == "songs" else "ytmusic_video"

    def search_query(self, query: str) -> list[Candidate]:
        results = self.client.search(query, self.kind, self.limit)
        found = (candidate_from_result(r, self.source) for r in results)
        return [c for c in found if c is not None][: self.limit]

    def search(self, track: Track) -> list[Candidate]:
        return self.search_query(build_query(track))


def track_from_candidate(candidate: Candidate) -> Track:
    """Metadata for a plain search (no Spotify): what YouTube Music tells us."""
    return Track(
        title=candidate.title,
        artists=candidate.artists,
        album=candidate.album,
        album_artist=candidate.artists[0] if candidate.artists else None,
        duration_ms=candidate.duration_s * 1000,
        cover_url=candidate.thumbnail_url,
    )
