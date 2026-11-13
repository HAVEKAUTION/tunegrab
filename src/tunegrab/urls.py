"""Parse Spotify URLs/URIs into (kind, id), and YouTube URLs into video IDs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, urlsplit

SpotifyKind = Literal["track", "album", "playlist", "artist", "liked"]

_KINDS: frozenset[str] = frozenset({"track", "album", "playlist", "artist"})
_SPOTIFY_ID = re.compile(r"^[A-Za-z0-9]{22}$")
_INTL = re.compile(r"^intl-[a-z]{2}(?:[-_][A-Za-z]{2,4})?$", re.IGNORECASE)
_SPOTIFY_HOSTS = frozenset({"open.spotify.com", "play.spotify.com"})
SHORT_LINK_HOSTS = frozenset({"spotify.link", "spoti.fi"})

_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"}
)


class InvalidURLError(ValueError):
    """Input is not a recognisable Spotify / YouTube link."""


@dataclass(frozen=True)
class SpotifyRef:
    kind: SpotifyKind
    id: str  # "" for liked songs

    @property
    def url(self) -> str:
        if self.kind == "liked":
            return "https://open.spotify.com/collection/tracks"
        return f"https://open.spotify.com/{self.kind}/{self.id}"

    @property
    def uri(self) -> str:
        return (
            "spotify:user:collection" if self.kind == "liked" else f"spotify:{self.kind}:{self.id}"
        )


def _host(text: str) -> str:
    host = (urlsplit(text).hostname or "").lower()
    return host.removeprefix("www.")


def _with_scheme(text: str) -> str:
    return text if "://" in text else "https://" + text


def is_spotify_input(text: str) -> bool:
    """Does this look like a Spotify link/URI at all (valid or not)?"""
    text = text.strip()
    if text.lower().startswith("spotify:"):
        return True
    host = _host(_with_scheme(text))
    return host in _SPOTIFY_HOSTS or host in SHORT_LINK_HOSTS


# Sites people paste without "https://"; anything else needs a scheme or "www." so
# artist names like "will.i.am" aren't mistaken for links.
_BARE_VIDEO_HOSTS = frozenset(
    {
        "youtu.be",
        "youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "vimeo.com",
        "soundcloud.com",
        "twitch.tv",
        "tiktok.com",
        "x.com",
        "twitter.com",
        "instagram.com",
        "facebook.com",
        "dailymotion.com",
        "bandcamp.com",
        "reddit.com",
        "bilibili.com",
    }
)


def is_video_link(text: str) -> bool:
    """A web link that isn't Spotify: a job for yt-dlp (video or audio download)."""
    text = text.strip()
    if not text or " " in text or is_spotify_input(text):
        return False
    lowered = text.lower()
    if lowered.startswith(("http://", "https://")):
        return bool(urlsplit(text).hostname)
    host = _host(_with_scheme(text))
    if lowered.startswith("www.") and "." in host:
        return True
    return host in _BARE_VIDEO_HOSTS or any(host.endswith("." + h) for h in _BARE_VIDEO_HOSTS)


def is_short_link(text: str) -> bool:
    return _host(_with_scheme(text.strip())) in SHORT_LINK_HOSTS


def _ref(kind: str, item_id: str, original: str) -> SpotifyRef:
    if kind not in _KINDS:
        raise InvalidURLError(f"Unsupported Spotify link type {kind!r}: {original}")
    if not _SPOTIFY_ID.match(item_id):
        raise InvalidURLError(
            f"That Spotify link looks cut off ({original}). "
            "Copy it again in Spotify: Share, then Copy link."
        )
    return SpotifyRef(kind, item_id)  # type: ignore[arg-type]


def parse_spotify_url(text: str) -> SpotifyRef:
    """Parse ``open.spotify.com`` URLs (incl. ``intl-xx/``, ``embed/``, ``?si=``) and URIs."""
    original = text
    text = text.strip()
    if not text:
        raise InvalidURLError("Empty input")

    if text.lower().startswith("spotify:"):
        parts = [p for p in text.split(":") if p]
        if "collection" in parts:
            return SpotifyRef("liked", "")
        for index, part in enumerate(parts[:-1]):
            if part.lower() in _KINDS:
                return _ref(part.lower(), parts[index + 1].split("?")[0], original)
        raise InvalidURLError(f"Unrecognised Spotify URI: {original}")

    url = _with_scheme(text)
    if _host(url) not in _SPOTIFY_HOSTS:
        raise InvalidURLError(f"Not a Spotify URL: {original}")
    segments = [s for s in urlsplit(url).path.split("/") if s]
    while segments and (_INTL.match(segments[0]) or segments[0] in ("embed", "embed-legacy")):
        segments.pop(0)
    if segments[:2] == ["collection", "tracks"]:
        return SpotifyRef("liked", "")
    if len(segments) >= 2 and segments[0] == "user":  # /user/<name>/playlist/<id>
        segments = segments[2:]
    if len(segments) < 2:
        raise InvalidURLError(f"Spotify URL has no item ID: {original}")
    return _ref(segments[0].lower(), segments[1], original)


def parse_youtube_id(text: str) -> str:
    """Return the 11-char video ID from a YouTube / YouTube Music URL or a bare ID."""
    text = text.strip()
    if _YOUTUBE_ID.match(text):
        return text
    url = _with_scheme(text)
    parts = urlsplit(url)
    host = _host(url)
    segments = [s for s in parts.path.split("/") if s]
    candidate: str | None = None
    if host == "youtu.be" and segments:
        candidate = segments[0]
    elif host in _YOUTUBE_HOSTS:
        if segments[:1] == ["watch"]:
            values = parse_qs(parts.query).get("v")
            candidate = values[0] if values else None
        elif len(segments) >= 2 and segments[0] in ("shorts", "embed", "live", "v"):
            candidate = segments[1]
    if candidate and _YOUTUBE_ID.match(candidate):
        return candidate
    raise InvalidURLError(f"Not a YouTube video link: {text}")
