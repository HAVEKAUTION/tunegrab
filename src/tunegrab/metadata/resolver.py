"""Resolve user input to a Collection: API provider first, then the embed fallback."""

from __future__ import annotations

import logging
import re

from tunegrab.config import Settings
from tunegrab.metadata.base import MetadataError, MetadataProvider, NotFoundError, fetch
from tunegrab.metadata.spotify_api import SpotifyAPIProvider
from tunegrab.metadata.spotify_embed import SpotifyEmbedProvider
from tunegrab.models import Collection, Track
from tunegrab.net import BROWSER_USER_AGENT, HttpClient, NetworkError
from tunegrab.urls import InvalidURLError, SpotifyRef, is_short_link, parse_spotify_url

log = logging.getLogger(__name__)

_OPEN_URL = re.compile(r"https://open\.spotify\.com/[^\s\"'<>]+")


class Resolver:
    def __init__(self, providers: list[MetadataProvider], http: HttpClient) -> None:
        if not providers:
            raise ValueError("at least one metadata provider is required")
        self.providers = providers
        self.http = http

    @classmethod
    def from_settings(cls, settings: Settings, http: HttpClient) -> Resolver:
        providers: list[MetadataProvider] = []
        api = SpotifyAPIProvider.from_settings(settings, http)
        if api is not None:
            providers.append(api)
        providers.append(SpotifyEmbedProvider(http))
        return cls(providers, http)

    @property
    def provider_names(self) -> list[str]:
        return [p.name for p in self.providers]

    def expand_short_link(self, text: str) -> str:
        """Follow ``spotify.link`` / ``spoti.fi`` redirects to the open.spotify.com URL."""
        url = text if "://" in text else "https://" + text
        try:
            response = self.http.get(url, headers={"User-Agent": BROWSER_USER_AGENT})
        except NetworkError as exc:
            raise MetadataError(f"Could not expand short link {text}: {exc}") from exc
        final = str(response.url)
        if "open.spotify.com" in final:
            return final
        found = _OPEN_URL.search(response.text)
        if found:
            return found.group(0)
        raise InvalidURLError(f"Short link {text} did not lead to a Spotify page")

    def parse(self, text: str) -> SpotifyRef:
        text = text.strip()
        if is_short_link(text):
            text = self.expand_short_link(text)
        return parse_spotify_url(text)

    def resolve(self, text: str) -> Collection:
        return self.resolve_ref(self.parse(text))

    def resolve_ref(self, ref: SpotifyRef) -> Collection:
        errors: list[str] = []
        not_found = 0
        for provider in self.providers:
            try:
                collection = fetch(provider, ref)
            except MetadataError as exc:
                log.info("%s could not fetch %s: %s", provider.name, ref.uri, exc)
                errors.append(f"{provider.name}: {exc}")
                not_found += isinstance(exc, NotFoundError)
                continue
            if errors:
                collection.warnings.insert(0, f"Used {provider.name} because " + "; ".join(errors))
            return collection
        error_type = NotFoundError if not_found == len(self.providers) else MetadataError
        raise error_type(" | ".join(errors))

    def enrich(self, track: Track) -> Track:
        """Fill in missing album details; never raises."""
        if not track.spotify_id or not track.is_incomplete:
            return track
        for provider in self.providers:
            try:
                track = provider.enrich(track)
            except MetadataError as exc:
                log.debug("%s could not enrich %s: %s", provider.name, track.spotify_id, exc)
                continue
            if not track.is_incomplete:
                break
        return track
