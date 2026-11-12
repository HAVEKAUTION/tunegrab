"""In-memory fakes for the pipeline, CLI and web tests (no network, no yt-dlp)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from tunegrab.config import Settings
from tunegrab.library.archive import Archive
from tunegrab.matching.base import SearchError
from tunegrab.matching.matcher import Matcher
from tunegrab.matching.ytmusic import YTMusicClient, YTMusicProvider
from tunegrab.metadata.base import NotFoundError
from tunegrab.metadata.resolver import Resolver
from tunegrab.models import Candidate, Collection, Track
from tunegrab.net import HttpClient
from tunegrab.pipeline import Pipeline
from tunegrab.services import Services

PLAYLIST_ID = "37i9dQZF1DXcBWIGoYBM5M"
ALBUM_ID = "4m2880jivSbbyEGAKfITCa"


def make_track(n: int, **extra: Any) -> Track:
    base: dict[str, Any] = {
        "spotify_id": f"{n:022d}",
        "title": f"Song {n}",
        "artists": [f"Artist {n}"],
        "album": f"Album {n}",
        "album_artist": f"Artist {n}",
        "track_number": n,
        "release_date": "2020-01-01",
        "duration_ms": 200_000,
        "cover_url": f"https://img.example/{n}.jpg",
    }
    base.update(extra)
    return Track(**base)


class FakeMetadata:
    name = "fake"

    def __init__(self, tracks: list[Track] | None = None) -> None:
        self.tracks = tracks if tracks is not None else [make_track(i) for i in range(1, 4)]
        self.enriched: list[str] = []

    def get_track(self, track_id: str) -> Track:
        for track in self.tracks:
            if track.spotify_id == track_id:
                return track
        raise NotFoundError(track_id)

    def get_album(self, album_id: str) -> Collection:
        return Collection(
            kind="album", id=album_id, name="Fake Album", tracks=self.tracks, source=self.name
        )

    def get_playlist(self, playlist_id: str) -> Collection:
        return Collection(
            kind="playlist",
            id=playlist_id,
            name="Fake Playlist",
            tracks=list(self.tracks),
            source=self.name,
        )

    def get_artist(self, artist_id: str) -> Collection:
        return Collection(
            kind="artist", id=artist_id, name="Fake Artist", tracks=self.tracks, source=self.name
        )

    def get_liked(self) -> Collection:
        raise NotFoundError("no liked songs")

    def enrich(self, track: Track) -> Track:
        self.enriched.append(track.key)
        return track.model_copy(
            update={
                "album": track.album or "Enriched",
                "release_date": track.release_date or "2001",
                "cover_url": track.cover_url or "https://x/c.jpg",
            }
        )


def candidate_for(track: Track, **extra: Any) -> Candidate:
    data: dict[str, Any] = {
        "video_id": (track.spotify_id or "x" * 11)[-11:],
        "title": track.title,
        "artists": track.artists,
        "duration_s": round(track.duration_s),
        "source": "ytmusic_song",
    }
    data.update(extra)
    return Candidate(**data)


class FakeAudio:
    """AudioProvider returning a perfect candidate unless told otherwise."""

    def __init__(
        self,
        name: str = "fake_audio",
        results: dict[str, list[Candidate]] | None = None,
        fail: bool = False,
    ) -> None:
        self.name = name
        self.results = results
        self.fail = fail
        self.calls = 0

    def search(self, track: Track) -> list[Candidate]:
        self.calls += 1
        if self.fail:
            raise SearchError("boom")
        if self.results is not None:
            return self.results.get(track.title, [])
        return [candidate_for(track)]


class FakeYTMusicClient:
    """Stands in for ytmusicapi.YTMusic."""

    def __init__(self, results: list[dict[str, Any]] | None = None) -> None:
        self.results = results or [
            {
                "videoId": "abcdefghijk",
                "title": "Found Song",
                "duration_seconds": 180,
                "artists": [{"name": "Found Artist"}],
                "album": {"name": "Found Album"},
                "thumbnails": [{"url": "https://lh3/x=w60-h60", "width": 60, "height": 60}],
            }
        ]

    def search(self, query: str, filter: str, limit: int) -> list[dict[str, Any]]:
        if filter == "artists":
            return [
                {"artist": "Someone Else", "browseId": "UCother"},
                {"artist": "Fake Band", "browseId": "UCfake"},
            ]
        return self.results

    def get_artist(self, channel: str) -> dict[str, Any]:
        thumbs = [{"url": "https://lh3/a=w60-h60", "width": 60, "height": 60}]
        return {
            "name": "Fake Band",
            "thumbnails": thumbs,
            "albums": {
                "browseId": "MPADfake",
                "params": "p1",
                "results": [{"title": "Only Shown", "browseId": "MPREb_shown", "year": "2020"}],
            },
            "singles": {
                "results": [
                    {
                        "title": "Hit Single",
                        "browseId": "MPREb_single",
                        "year": "2021",
                        "type": "Single",
                    },
                    {"title": "Old EP", "browseId": "MPREb_ep", "year": "2009", "type": "EP"},
                ]
            },
        }

    def get_artist_albums(self, browse_id: str, params: str, limit: int | None = None):  # type: ignore[no-untyped-def]
        return [
            {
                "title": "Debut",
                "browseId": "MPREb_debut",
                "year": "2001",
                "thumbnails": [{"url": "https://lh3/d=w226-h226", "width": 226, "height": 226}],
            },
            {"title": "Second", "browseId": "MPREb_second", "year": "2005"},
            {"title": "Debut", "browseId": "MPREb_debut", "year": "2001"},
        ]

    def get_album(self, browse_id: str) -> dict[str, Any]:
        if browse_id == "MPREb_missing":
            return {"tracks": []}
        return {
            "title": "Debut",
            "year": "2001",
            "trackCount": 3,
            "artists": [{"name": "Fake Band"}],
            "thumbnails": [{"url": "https://lh3/c=w544-h544", "width": 544, "height": 544}],
            "tracks": [
                {
                    "videoId": "vid_one_001",
                    "title": "One",
                    "artists": [{"name": "Fake Band"}],
                    "trackNumber": 1,
                    "duration_seconds": 200,
                    "isAvailable": True,
                },
                {"videoId": None, "title": "Unavailable", "trackNumber": 2, "isAvailable": False},
                {
                    "videoId": "vid_three03",
                    "title": "Three",
                    "artists": [],
                    "trackNumber": 3,
                    "duration_seconds": 150,
                    "isExplicit": True,
                },
            ],
        }


class FakeDownloader:
    def __init__(
        self,
        fail_times: int = 0,
        block: threading.Event | None = None,
        started: threading.Event | None = None,
    ) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self.block = block
        self.started = started
        self.lock = threading.Lock()

    def download(
        self,
        video_id: str,
        dest_dir: Path,
        progress: Any = None,
        cancel: threading.Event | None = None,
    ) -> Path:
        from tunegrab.download.ytdlp import DownloadCancelled, DownloadError

        with self.lock:
            self.calls += 1
            should_fail = self.calls <= self.fail_times
        out = dest_dir / f"{video_id}.mp3"
        out.write_bytes(b"partial")
        if self.started is not None:
            self.started.set()
        if self.block is not None:
            while not self.block.wait(0.01):
                if cancel is not None and cancel.is_set():
                    raise DownloadCancelled("cancelled")
        if should_fail:
            raise DownloadError("network hiccup")
        if progress:
            progress(0.5, "downloading")
            progress(1.0, "converting")
        out.write_bytes(b"audio")
        return out


class FakeTagger:
    def __init__(self) -> None:
        self.tagged: list[tuple[Path, Track, bytes | None, str | None]] = []

    def tag(
        self, path: Path, track: Track, cover: bytes | None = None, lyrics: str | None = None
    ) -> None:
        self.tagged.append((path, track, cover, lyrics))


def make_pipeline(
    tmp: Path,
    *,
    tracks: list[Track] | None = None,
    downloader: FakeDownloader | None = None,
    audio: FakeAudio | None = None,
    **settings: Any,
) -> tuple[Pipeline, Archive]:
    archive = Archive(tmp / "archive.db")
    metadata = FakeMetadata(tracks)
    config = Settings(_env_file=None, output_dir=tmp / "lib", lyrics=False, **settings)
    pipeline = Pipeline(
        settings=config,
        resolver=Resolver([metadata], HttpClient()),
        matcher=Matcher([audio or FakeAudio()], cache=archive),
        downloader=downloader or FakeDownloader(),
        tagger=FakeTagger(),
        archive=archive,
        cover_fetcher=lambda url: b"\xff\xd8\xffcover",
        retry_backoff=0.0,
    )
    return pipeline, archive


class FakeServices(Services):
    """Real Services wiring with every network-facing piece faked."""

    def __init__(self, settings: Settings, tracks: list[Track] | None = None) -> None:
        archive = Archive(settings.resolved_archive_path)
        client = YTMusicClient(FakeYTMusicClient())
        songs = YTMusicProvider(client, "songs")
        videos = YTMusicProvider(client, "videos")
        self.metadata = FakeMetadata(tracks)
        self.downloader = FakeDownloader()
        self.tagger = FakeTagger()
        super().__init__(
            settings=settings,
            http=HttpClient(),
            archive=archive,
            resolver=Resolver([self.metadata], HttpClient()),
            songs=songs,
            videos=videos,
            youtube=FakeYouTube(),  # type: ignore[arg-type]
            matcher=Matcher([FakeAudio()], cache=archive),
        )

    def pipeline(self, on_event: Any = None, settings: Settings | None = None) -> Pipeline:
        return Pipeline(
            settings=settings or self.settings,
            resolver=self.resolver,
            matcher=self.matcher,
            downloader=self.downloader,
            tagger=self.tagger,
            archive=self.archive,
            cover_fetcher=lambda url: b"",
            query_lookup=self.lookup_query,
            on_event=on_event,
            retry_backoff=0.0,
        )


class FakeYouTube:
    name = "youtube"

    def search(self, track: Track) -> list[Candidate]:
        return []

    def video(self, video_id: str) -> Candidate:
        return Candidate(
            video_id=video_id,
            title="Manual Pick",
            artists=["Someone"],
            duration_s=200,
            source="youtube",
        )
