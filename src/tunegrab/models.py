"""Pydantic models: Track, Candidate, Match, Collection, TaskStatus, TrackTask, Job."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

CollectionKind = Literal[
    "track", "album", "playlist", "artist", "liked", "search", "selection", "video"
]
CandidateSource = Literal["ytmusic_song", "ytmusic_video", "youtube", "manual"]


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(UTC)


class Track(BaseModel):
    spotify_id: str | None = None
    title: str
    artists: list[str] = Field(default_factory=list)  # first = primary
    album: str | None = None
    album_artist: str | None = None
    album_id: str | None = None
    track_number: int | None = None
    disc_number: int | None = None
    total_tracks: int | None = None
    release_date: str | None = None  # "YYYY" or "YYYY-MM-DD"
    duration_ms: int = 0
    isrc: str | None = None
    explicit: bool = False
    cover_url: str | None = None
    genres: list[str] = Field(default_factory=list)

    @property
    def primary_artist(self) -> str:
        return self.artists[0] if self.artists else "Unknown Artist"

    @property
    def year(self) -> str | None:
        return self.release_date[:4] if self.release_date else None

    @property
    def duration_s(self) -> float:
        return self.duration_ms / 1000

    @property
    def spotify_url(self) -> str | None:
        return f"https://open.spotify.com/track/{self.spotify_id}" if self.spotify_id else None

    @property
    def display(self) -> str:
        return f"{', '.join(self.artists) or 'Unknown Artist'} - {self.title}"

    @property
    def key(self) -> str:
        """Stable identity for the archive: the Spotify ID, or a normalised query."""
        if self.spotify_id:
            return self.spotify_id
        base = "q:" + " ".join(f"{self.primary_artist} - {self.title}".lower().split())
        # YouTube Music album tracks: the same song on another release is a different file
        return f"{base} @{self.album_id}" if self.album_id else base

    @property
    def is_incomplete(self) -> bool:
        """True when album-level metadata is missing (e.g. from the embed fallback)."""
        return self.album is None or self.release_date is None or self.cover_url is None


class Candidate(BaseModel):
    video_id: str
    title: str
    artists: list[str] = Field(default_factory=list)
    duration_s: int = 0
    source: CandidateSource
    isrc: str | None = None
    album: str | None = None
    thumbnail_url: str | None = None

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


class Alternative(BaseModel):
    """Another video the matcher found for a song, kept so the user can swap to it."""

    candidate: Candidate
    score: float
    reason: str


class Match(BaseModel):
    track: Track
    candidate: Candidate | None
    score: float  # 0-100
    reason: str  # human-readable why
    cached: bool = False
    alternatives: list[Alternative] = Field(default_factory=list)  # next-best, best first

    def accepted(self, threshold: float) -> bool:
        return self.candidate is not None and self.score >= threshold


class Collection(BaseModel):
    """What a URL resolved to: one track, or an album / playlist / artist's tracks."""

    kind: CollectionKind
    id: str | None = None
    name: str
    owner: str | None = None
    tracks: list[Track] = Field(default_factory=list)
    total: int | None = None  # how many tracks the source says exist
    skipped: int = 0  # null / local / podcast items left out
    warnings: list[str] = Field(default_factory=list)
    source: str = ""  # which provider produced it

    @property
    def is_playlist_like(self) -> bool:
        return self.kind in ("playlist", "liked")


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RESOLVING = "resolving"
    MATCHING = "matching"
    DOWNLOADING = "downloading"
    CONVERTING = "converting"
    TAGGING = "tagging"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"

    @property
    def finished(self) -> bool:
        return self in (TaskStatus.DONE, TaskStatus.SKIPPED, TaskStatus.FAILED)


class TrackTask(BaseModel):
    id: str = Field(default_factory=_new_id)
    track: Track
    position: int = 0  # 1-based position within its collection
    status: TaskStatus = TaskStatus.QUEUED
    match: Match | None = None
    path: str | None = None
    error: str | None = None
    failed_stage: str | None = None
    attempts: int = 0
    progress: float = 0.0  # 0..1 during download
    message: str = ""
    source: str | None = None  # what YouTube served, e.g. "Opus 129 kbps"
    low_quality: bool = False  # only a low-quality source was available
    review: str | None = None  # why this match deserves a second look ("Check this match")
    # the library file this download takes the place of (the user chose another match);
    # it is only replaced once the new file is tagged and ready
    replaces: str | None = None


class Job(BaseModel):
    id: str = Field(default_factory=_new_id)
    input: str
    collection: Collection | None = None
    tasks: list[TrackTask] = Field(default_factory=list)
    # audio format chosen for this job (None: the default in Settings); retries reuse it
    format: str | None = None
    created_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None
    cancelled: bool = False
    error: str | None = None
    # ids of failed tasks already handed to a retry job (each failure is retried once)
    retried: set[str] = Field(default_factory=set)
    # set while YouTube has blocked downloads from this connection (rate limit)
    paused_until: datetime | None = None

    @property
    def name(self) -> str:
        return self.collection.name if self.collection else self.input

    def retryable(self) -> list[TrackTask]:
        """Failed tasks that haven't been retried yet."""
        return [t for t in self.tasks if t.status == TaskStatus.FAILED and t.id not in self.retried]

    def counts(self) -> dict[str, int]:
        result = {status.value: 0 for status in TaskStatus}
        for task in self.tasks:
            result[task.status.value] += 1
        return result
