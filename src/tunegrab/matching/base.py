"""AudioProvider protocol: search(track) -> list[Candidate]."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tunegrab.errors import TuneGrabError
from tunegrab.models import Candidate, Track


class SearchError(TuneGrabError):
    """An audio provider's search failed (network, parse error, ...)."""


@runtime_checkable
class AudioProvider(Protocol):
    name: str

    def search(self, track: Track) -> list[Candidate]: ...


def build_query(track: Track, max_artists: int = 2) -> str:
    artists = " ".join(track.artists[:max_artists]) if track.artists else ""
    return f"{artists} {track.title}".strip()


def parse_duration(text: str | None) -> int:
    """``"3:45"`` / ``"1:02:03"`` -> seconds; 0 if unknown."""
    if not text:
        return 0
    total = 0
    try:
        for part in text.strip().split(":"):
            total = total * 60 + int(part)
    except ValueError:
        return 0
    return total
