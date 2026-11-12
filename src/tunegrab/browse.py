"""Browsing for the web UI: an artist's releases (to tick and queue) and release track lists.

Sources, best first:

* Spotify Web API (with credentials): the complete discography.
* Spotify's public artist page (no credentials): the newest 8 albums, 8 singles and
  1 compilation, plus top tracks. Spotify embeds this as base64 JSON (``initialState``).
* YouTube Music (no credentials): the complete discography with video IDs, used when the
  Spotify list is incomplete. Tracks from it skip matching (the video is known).

Plain text goes to YouTube Music search: an artist's name opens the artist, anything else
("Discovery Daft Punk", "One More Time") lists matching albums and songs.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from tunegrab.matching.base import SearchError
from tunegrab.matching.ytmusic import YTMusicClient, best_thumbnail
from tunegrab.metadata.base import MetadataError, NotFoundError
from tunegrab.models import Candidate, Collection, Match, Track
from tunegrab.net import BROWSER_USER_AGENT, HttpClient, NetworkError

log = logging.getLogger(__name__)

ReleaseKind = Literal["album", "single", "compilation"]
ReleaseSource = Literal["spotify", "ytmusic"]
_INITIAL_STATE = re.compile(r'<script id="initialState" type="text/plain">([^<]+)</script>')
_KINDS: dict[str, ReleaseKind] = {
    "album": "album",
    "single": "single",
    "ep": "single",
    "compilation": "compilation",
}


class Release(BaseModel):
    id: str
    name: str
    kind: ReleaseKind = "album"
    year: str | None = None
    cover_url: str | None = None
    source: ReleaseSource = "spotify"
    total_tracks: int | None = None
    artist: str | None = None  # set in search results, where releases mix artists


class ArtistOverview(BaseModel):
    id: str | None = None
    name: str
    image_url: str | None = None
    source: str
    complete: bool  # False when the list is known to be cut short
    releases: list[Release] = Field(default_factory=list)
    top_tracks: list[Track] = Field(default_factory=list)
    note: str | None = None


class SongHit(BaseModel):
    """A song from YouTube Music search; queueable as is (the video is known)."""

    id: str  # video ID
    title: str
    artists: list[str] = Field(default_factory=list)
    album: str | None = None
    album_id: str | None = None
    duration_s: int | None = None
    explicit: bool = False
    cover_url: str | None = None


class ArtistHit(BaseModel):
    id: str  # YouTube Music channel ID
    name: str


class SearchResults(BaseModel):
    query: str
    artist: ArtistHit | None = None  # an artist the text may also mean
    releases: list[Release] = Field(default_factory=list)
    songs: list[SongHit] = Field(default_factory=list)


ARTIST_MATCH = 90  # text this close to the top result's artist name opens the artist
MAX_SONGS = 10
MAX_ALBUMS = 12


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


def _names(runs: Any) -> list[str]:
    return [str(a["name"]) for a in runs or [] if isinstance(a, dict) and a.get("name")]


def _artist_of(result: dict[str, Any]) -> ArtistHit | None:
    runs = [a for a in result.get("artists") or [] if isinstance(a, dict) and a.get("name")]
    name = runs[0]["name"] if runs else result.get("artist")
    channel = (runs[0].get("id") if runs else None) or result.get("browseId")
    if not name or not channel:
        return None
    return ArtistHit(id=str(channel), name=str(name))


def _named_artist(text: str, results: list[dict[str, Any]]) -> ArtistHit | None:
    top = results[0] if results else {}
    if top.get("resultType") != "artist":
        return None
    hit = _artist_of(top)
    if hit is None or fuzz.ratio(_norm(text), _norm(hit.name)) < ARTIST_MATCH:
        return None
    return hit


def song_match(hit: SongHit) -> Match:
    """A search hit as a ready-made match, so the download skips matching."""
    track = Track(
        title=hit.title,
        artists=hit.artists,
        album=hit.album,
        album_artist=hit.artists[0] if hit.artists else None,
        album_id=hit.album_id,
        duration_ms=(hit.duration_s or 0) * 1000,
        explicit=hit.explicit,
        cover_url=hit.cover_url,
    )
    candidate = Candidate(
        video_id=hit.id,
        title=hit.title,
        artists=hit.artists,
        duration_s=hit.duration_s or 0,
        source="ytmusic_song",
        album=hit.album,
        thumbnail_url=hit.cover_url,
    )
    return Match(track=track, candidate=candidate, score=100, reason="picked from search")


def _kind(value: str | None) -> ReleaseKind:
    return _KINDS.get((value or "album").lower(), "album")


def _largest(sources: list[dict[str, Any]] | None) -> str | None:
    if not sources:
        return None
    best = max(sources, key=lambda s: (s.get("width") or 0) * (s.get("height") or 0))
    url = best.get("url")
    return str(url) if url else None


def _id(uri: str | None) -> str | None:
    return uri.rsplit(":", 1)[-1] if uri else None


# --- Spotify public artist page ---------------------------------------------------------
def parse_artist_page(page: str, artist_id: str) -> ArtistOverview:
    match = _INITIAL_STATE.search(page)
    if not match:
        raise MetadataError("Spotify artist page had no data (layout changed?)")
    try:
        state = json.loads(base64.b64decode(match.group(1)))
    except (binascii.Error, ValueError) as exc:
        raise MetadataError(f"Could not decode Spotify artist page: {exc}") from exc
    entity = (state.get("entities") or {}).get("items", {}).get(f"spotify:artist:{artist_id}")
    if not isinstance(entity, dict) or "discography" not in entity:
        raise NotFoundError(f"Spotify artist {artist_id} not found")
    disc = entity["discography"]
    releases: list[Release] = []
    seen: set[str] = set()
    for group in ("albums", "singles", "compilations"):
        for item in (disc.get(group) or {}).get("items") or []:
            for rel in (item.get("releases") or {}).get("items") or []:
                rid = _id(rel.get("uri"))
                if not rid or rid in seen or not rel.get("name"):
                    continue
                seen.add(rid)
                year = (rel.get("date") or {}).get("year")
                releases.append(
                    Release(
                        id=rid,
                        name=rel["name"],
                        kind=_kind(rel.get("type")),
                        year=str(year) if year else None,
                        cover_url=_largest((rel.get("coverArt") or {}).get("sources")),
                    )
                )
    top: list[Track] = []
    for item in (disc.get("topTracks") or {}).get("items") or []:
        track = item.get("track") or {}
        tid = _id(track.get("uri"))
        if not tid or not track.get("name"):
            continue
        album = track.get("albumOfTrack") or {}
        top.append(
            Track(
                spotify_id=tid,
                title=track["name"],
                artists=[
                    a["profile"]["name"]
                    for a in (track.get("artists") or {}).get("items") or []
                    if (a.get("profile") or {}).get("name")
                ],
                album_id=_id(album.get("uri")),
                cover_url=_largest((album.get("coverArt") or {}).get("sources")),
                explicit=(track.get("contentRating") or {}).get("label") == "EXPLICIT",
            )
        )
    name = (entity.get("profile") or {}).get("name") or artist_id
    image = _largest(((entity.get("visuals") or {}).get("avatarImage") or {}).get("sources"))
    return ArtistOverview(
        id=artist_id,
        name=name,
        image_url=image,
        source="spotify-page",
        complete=False,  # the page only lists the newest releases of each kind
        releases=releases,
        top_tracks=top,
        note=(
            "Spotify's public page lists only the newest 8 albums and 8 singles. "
            "Use “Full discography” to load every release from YouTube Music."
        ),
    )


# --- Spotify Web API -------------------------------------------------------------------
def api_artist_overview(api: Any, artist_id: str) -> ArtistOverview:
    """Complete discography through ``SpotifyAPIProvider._get/_paginate``."""
    artist = api._get(f"/artists/{artist_id}")
    pages = api._paginate(
        api._get(
            f"/artists/{artist_id}/albums",
            params={"include_groups": "album,single,compilation", "limit": 10},
        )
    )
    releases = [
        Release(
            id=a["id"],
            name=a.get("name") or a["id"],
            kind=_kind(a.get("album_type")),
            year=(a.get("release_date") or "")[:4] or None,
            cover_url=_largest(a.get("images")),
            total_tracks=a.get("total_tracks"),
        )
        for a in pages
        if a.get("id")
    ]
    return ArtistOverview(
        id=artist_id,
        name=artist.get("name") or artist_id,
        image_url=_largest(artist.get("images")),
        source="spotify-api",
        complete=True,
        releases=releases,
    )


# --- YouTube Music ---------------------------------------------------------------------
def _year(value: Any) -> str | None:
    text = str(value or "")
    found = re.search(r"\b(19|20)\d{2}\b", text)
    return found.group(0) if found else None


class YTMusicCatalog:
    def __init__(self, client: YTMusicClient) -> None:
        self.client = client

    def find_artist(self, name: str) -> str:
        results = self.client.call("search", name, filter="artists", limit=5) or []
        best: tuple[float, str] | None = None
        for result in results:
            browse_id = result.get("browseId")
            if not browse_id:
                continue
            score = fuzz.WRatio(name.lower(), str(result.get("artist") or "").lower())
            if best is None or score > best[0]:
                best = (score, browse_id)
        if best is None or best[0] < 80:
            raise NotFoundError(f"No YouTube Music artist named {name!r}")
        return best[1]

    def lookup(self, text: str) -> ArtistOverview | SearchResults:
        """The artist when the text is their name (YouTube Music's top result), otherwise
        the albums and songs that match."""
        results = self._everything(text)
        named = _named_artist(text, results)
        if named is not None:
            return self.artist(named.name, channel=named.id)
        suggestion = None
        for result in results:
            hit = _artist_of(result) if result.get("resultType") == "artist" else None
            if hit and fuzz.partial_ratio(_norm(hit.name), _norm(text)) >= ARTIST_MATCH:
                suggestion = hit
                break
        return self.search(text, suggestion)

    def named_artist(self, text: str) -> ArtistHit | None:
        """The artist the text names, if that's what YouTube Music puts on top."""
        return _named_artist(text, self._everything(text))

    def _everything(self, text: str) -> list[dict[str, Any]]:
        found = self.client.call("search", text, limit=10) or []
        return [r for r in found if isinstance(r, dict)]

    def search(self, text: str, artist: ArtistHit | None = None) -> SearchResults:
        songs: list[SongHit] = []
        for item in self.client.call("search", text, filter="songs", limit=MAX_SONGS) or []:
            if not isinstance(item, dict) or not item.get("videoId") or len(songs) >= MAX_SONGS:
                continue
            album: dict[str, Any] = item["album"] if isinstance(item.get("album"), dict) else {}
            seconds = item.get("duration_seconds")
            songs.append(
                SongHit(
                    id=str(item["videoId"]),
                    title=str(item.get("title") or "Unknown"),
                    artists=_names(item.get("artists")),
                    album=album.get("name"),
                    album_id=album.get("id"),
                    duration_s=int(seconds) if seconds else None,
                    explicit=bool(item.get("isExplicit")),
                    cover_url=best_thumbnail(item.get("thumbnails")),
                )
            )
        releases: list[Release] = []
        for item in self.client.call("search", text, filter="albums", limit=MAX_ALBUMS) or []:
            if not isinstance(item, dict) or not item.get("browseId"):
                continue
            if len(releases) >= MAX_ALBUMS:
                break
            artists = _names(item.get("artists"))
            releases.append(
                Release(
                    id=str(item["browseId"]),
                    name=str(item.get("title") or item["browseId"]),
                    kind=_kind(str(item.get("type") or "album")),
                    year=_year(item.get("year")),
                    cover_url=best_thumbnail(item.get("thumbnails")),
                    source="ytmusic",
                    artist=", ".join(artists) or None,
                )
            )
        if not songs and not releases:
            raise NotFoundError(f"Nothing on YouTube Music matches \u201c{text}\u201d.")
        return SearchResults(query=text, artist=artist, releases=releases, songs=songs)

    def artist(self, name: str, channel: str | None = None) -> ArtistOverview:
        channel = channel or self.find_artist(name)
        data = self.client.call("get_artist", channel) or {}
        releases: list[Release] = []
        seen: set[str] = set()
        for group, kind in (("albums", "album"), ("singles", "single")):
            section = data.get(group) or {}
            items = section.get("results") or []
            if section.get("params") and section.get("browseId"):
                try:
                    items = (
                        self.client.call(
                            "get_artist_albums", section["browseId"], section["params"], limit=None
                        )
                        or items
                    )
                except SearchError as exc:
                    log.warning("YouTube Music %s list failed: %s", group, exc)
            for item in items:
                browse_id = item.get("browseId")
                if not browse_id or browse_id in seen:
                    continue
                seen.add(browse_id)
                item_type = str(item.get("type") or kind).lower()
                releases.append(
                    Release(
                        id=browse_id,
                        name=str(item.get("title") or browse_id),
                        kind=_kind("single" if item_type in ("single", "ep") else kind),
                        year=_year(item.get("year")),
                        cover_url=best_thumbnail(item.get("thumbnails")),
                        source="ytmusic",
                    )
                )
        return ArtistOverview(
            id=channel,
            name=str(data.get("name") or name),
            image_url=best_thumbnail(data.get("thumbnails")),
            source="ytmusic",
            complete=True,
            releases=releases,
        )

    def album(self, browse_id: str) -> tuple[Collection, dict[str, Match]]:
        """A YouTube Music album as a Collection plus a ready-made match per track."""
        data = self.client.call("get_album", browse_id) or {}
        if not data.get("tracks"):
            raise NotFoundError(f"YouTube Music album {browse_id} not found")
        album_artists = [a["name"] for a in data.get("artists") or [] if a.get("name")]
        cover = best_thumbnail(data.get("thumbnails"))
        year = _year(data.get("year"))
        playable = [t for t in data["tracks"] if t.get("videoId") and t.get("isAvailable", True)]
        tracks: list[Track] = []
        matches: dict[str, Match] = {}
        for number, item in enumerate(playable, start=1):
            artists = [a["name"] for a in item.get("artists") or [] if a.get("name")]
            seconds = item.get("duration_seconds") or 0
            track = Track(
                title=str(item.get("title") or "Unknown"),
                artists=artists or album_artists,
                album=str(data.get("title") or ""),
                album_artist=album_artists[0] if album_artists else None,
                album_id=browse_id,
                track_number=item.get("trackNumber") or number,
                total_tracks=data.get("trackCount") or len(playable),
                release_date=year,
                duration_ms=int(seconds) * 1000,
                explicit=bool(item.get("isExplicit")),
                cover_url=cover,
            )
            candidate = Candidate(
                video_id=str(item["videoId"]),
                title=track.title,
                artists=track.artists,
                duration_s=int(seconds),
                source="ytmusic_song",
                album=track.album,
                thumbnail_url=cover,
            )
            matches[candidate.video_id] = Match(
                track=track, candidate=candidate, score=100, reason="YouTube Music album track"
            )
            tracks.append(track)
        collection = Collection(
            kind="album",
            id=browse_id,
            name=str(data.get("title") or browse_id),
            owner=album_artists[0] if album_artists else None,
            tracks=tracks,
            source="ytmusic",
        )
        return collection, matches


# --- the facade used by the web app ------------------------------------------------------
class Browser:
    def __init__(self, http: HttpClient, ytmusic: YTMusicCatalog, api: Any | None = None) -> None:
        self.http = http
        self.ytmusic = ytmusic
        self.api = api

    def spotify_artist(self, artist_id: str) -> ArtistOverview:
        if self.api is not None:
            try:
                return api_artist_overview(self.api, artist_id)
            except MetadataError as exc:
                log.info("API artist listing failed (%s); using the public page", exc)
        try:
            response = self.http.get(
                f"https://open.spotify.com/artist/{artist_id}",
                headers={"User-Agent": BROWSER_USER_AGENT, "Accept-Language": "en"},
            )
        except NetworkError as exc:
            raise MetadataError(str(exc)) from exc
        if response.status_code == 404:
            raise NotFoundError(f"Spotify artist {artist_id} not found")
        if not response.is_success:
            raise MetadataError(f"Spotify artist page returned HTTP {response.status_code}")
        return parse_artist_page(response.content.decode("utf-8", "replace"), artist_id)
