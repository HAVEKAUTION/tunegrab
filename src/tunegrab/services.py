"""Wire the real providers together from Settings (shared by the CLI and the web UI)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

from tunegrab.browse import Browser, YTMusicCatalog
from tunegrab.config import Settings
from tunegrab.download.ytdlp import YtDlpDownloader
from tunegrab.library.archive import Archive
from tunegrab.matching.base import AudioProvider, SearchError
from tunegrab.matching.matcher import Matcher
from tunegrab.matching.youtube import YouTubeSearchProvider
from tunegrab.matching.ytmusic import YTMusicClient, YTMusicProvider, track_from_candidate
from tunegrab.metadata.resolver import Resolver
from tunegrab.metadata.spotify_api import SpotifyAPIProvider
from tunegrab.models import Match, Track
from tunegrab.net import HttpClient, get_client
from tunegrab.pipeline import Pipeline
from tunegrab.runner import EventCallback
from tunegrab.tagging.lyrics import LyricsClient
from tunegrab.tagging.tagger import MutagenTagger


@dataclass
class Services:
    settings: Settings
    http: HttpClient
    archive: Archive
    resolver: Resolver
    songs: YTMusicProvider
    videos: YTMusicProvider
    youtube: YouTubeSearchProvider
    matcher: Matcher

    def close(self) -> None:
        self.archive.close()

    @cached_property
    def browser(self) -> Browser:
        api = next((p for p in self.resolver.providers if isinstance(p, SpotifyAPIProvider)), None)
        return Browser(self.http, YTMusicCatalog(self.songs.client), api)

    def lookup_query(self, query: str) -> tuple[Track, Match] | None:
        """Free-text search: take the top YouTube Music song (then video) result."""
        for provider in (self.songs, self.videos):
            try:
                candidates = provider.search_query(query)
            except SearchError:
                continue
            if candidates:
                candidate = candidates[0]
                track = track_from_candidate(candidate)
                match = Match(
                    track=track,
                    candidate=candidate,
                    score=100.0,
                    reason=f"top {provider.name.replace('_', ' ')} result for the search",
                )
                return track, match
        return None

    def pipeline(
        self, on_event: EventCallback | None = None, settings: Settings | None = None
    ) -> Pipeline:
        settings = settings or self.settings
        return Pipeline(
            settings=settings,
            resolver=self.resolver,
            matcher=self.matcher,
            downloader=YtDlpDownloader(
                settings.format, settings.bitrate, settings.cookies_from_browser
            ),
            tagger=MutagenTagger(),
            archive=self.archive,
            lyrics=LyricsClient(self.http) if settings.lyrics else None,
            cover_fetcher=self.http.get_bytes,
            query_lookup=self.lookup_query,
            on_event=on_event,
        )


def build_services(settings: Settings, http: HttpClient | None = None) -> Services:
    http = http or get_client()
    archive = Archive(settings.resolved_archive_path)
    ytm = YTMusicClient()
    songs = YTMusicProvider(ytm, "songs")
    videos = YTMusicProvider(ytm, "videos")
    youtube = YouTubeSearchProvider()
    providers: list[AudioProvider] = [songs, videos, youtube]
    matcher = Matcher(providers, threshold=settings.match_threshold, cache=archive)
    return Services(
        settings=settings,
        http=http,
        archive=archive,
        resolver=Resolver.from_settings(settings, http),
        songs=songs,
        videos=videos,
        youtube=youtube,
        matcher=matcher,
    )
