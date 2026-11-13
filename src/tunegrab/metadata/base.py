"""MetadataProvider protocol, typed errors, and dispatch helpers."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tunegrab.errors import TuneGrabError
from tunegrab.models import Collection, Track
from tunegrab.urls import SpotifyRef

# Where users learn about the optional Spotify app (shown in the app and CLI alike).
CREDENTIALS_HELP = 'see "Spotify developer credentials" in the README'


class MetadataError(TuneGrabError):
    """Metadata could not be fetched from this provider."""


class NotFoundError(MetadataError):
    """The item does not exist (or is hidden from this provider)."""


class RestrictedError(MetadataError):
    """The provider refused access (Dev Mode limits, missing login, private item)."""


@runtime_checkable
class MetadataProvider(Protocol):
    name: str

    def get_track(self, track_id: str) -> Track: ...

    def get_album(self, album_id: str) -> Collection: ...

    def get_playlist(self, playlist_id: str) -> Collection: ...

    def get_artist(self, artist_id: str) -> Collection: ...

    def get_liked(self) -> Collection: ...

    def enrich(self, track: Track) -> Track:
        """Fill in missing album-level fields. Returns the (possibly new) track."""
        ...


def fetch(provider: MetadataProvider, ref: SpotifyRef) -> Collection:
    """Fetch whatever ``ref`` points at as a :class:`Collection`."""
    if ref.kind == "track":
        track = provider.get_track(ref.id)
        return Collection(
            kind="track", id=ref.id, name=track.display, tracks=[track], source=provider.name
        )
    if ref.kind == "album":
        return provider.get_album(ref.id)
    if ref.kind == "playlist":
        return provider.get_playlist(ref.id)
    if ref.kind == "artist":
        return provider.get_artist(ref.id)
    return provider.get_liked()
