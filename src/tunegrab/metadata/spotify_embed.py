"""Fallback metadata provider that needs no API key.

Reads the public embed pages (``open.spotify.com/embed/{kind}/{id}``), whose
``__NEXT_DATA__`` JSON carries track lists, artists, durations and cover art, plus the
``<meta>`` tags of the regular public pages for album name / track number / release date.

Limits (Spotify's, not ours): playlists expose at most their first 100 tracks, artists
only their top 10 tracks, and there is no ISRC or genre. Private playlists don't work.
"""

from __future__ import annotations

import html as htmllib
import json
import logging
import re
import threading
from typing import Any

from tunegrab.metadata.base import CREDENTIALS_HELP, MetadataError, NotFoundError, RestrictedError
from tunegrab.models import Collection, Track
from tunegrab.net import BROWSER_USER_AGENT, HttpClient, NetworkError

log = logging.getLogger(__name__)


EMBED_URL = "https://open.spotify.com/embed/{kind}/{id}"
PAGE_URL = "https://open.spotify.com/{kind}/{id}"
EMBED_PLAYLIST_LIMIT = 100

_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', re.S
)
_META = re.compile(r"<meta\s+[^>]*>", re.I)
_ATTR = re.compile(r'(\w[\w:-]*)\s*=\s*"([^"]*)"')
_SPOTIFY_ID_IN_URL = re.compile(r"/(?:album|track|artist|playlist)/([A-Za-z0-9]{22})")
# Embed track lists join artists with ",\xa0" (a no-break space), which keeps names like
# "Tyler, The Creator" (normal space) intact.
_ARTIST_SEPARATOR = ",\xa0"


def parse_entity(page: str) -> dict[str, Any]:
    """Extract ``props.pageProps.state.data.entity`` from an embed page."""
    match = _NEXT_DATA.search(page)
    if not match:
        raise MetadataError("Spotify embed page has no __NEXT_DATA__ (page format changed?)")
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise MetadataError(f"Spotify embed JSON is malformed: {exc}") from exc
    props = (data.get("props") or {}).get("pageProps") or {}
    if props.get("status") == 404:
        raise NotFoundError("Spotify says this item does not exist (or is private).")
    entity = ((props.get("state") or {}).get("data") or {}).get("entity")
    if not isinstance(entity, dict):
        raise MetadataError("Spotify embed JSON has no entity (page format changed?)")
    return entity


def parse_meta(page: str) -> dict[str, list[str]]:
    """Map ``<meta property|name=... content=...>`` keys to all their values, in order."""
    result: dict[str, list[str]] = {}
    for tag in _META.findall(page):
        attrs = dict(_ATTR.findall(tag))
        key = attrs.get("property") or attrs.get("name")
        if key and "content" in attrs:
            result.setdefault(key, []).append(htmllib.unescape(attrs["content"]))
    return result


def split_artists(subtitle: str | None) -> list[str]:
    if not subtitle:
        return []
    return [name.strip() for name in subtitle.split(_ARTIST_SEPARATOR) if name.strip()]


def _largest_image(entity: dict[str, Any]) -> str | None:
    images = (entity.get("visualIdentity") or {}).get("image") or []
    cover_art = entity.get("coverArt") or {}
    images = images + (cover_art.get("sources") or [])
    if not images:
        return None
    best = max(
        images,
        key=lambda i: (
            (i.get("maxWidth") or i.get("width") or 0)
            * (i.get("maxHeight") or i.get("height") or 0)
        ),
    )
    url = best.get("url")
    return str(url) if url else None


def _id_from_uri(uri: str | None) -> str | None:
    if uri and uri.count(":") == 2:
        return uri.rsplit(":", 1)[1]
    return None


def _release_date(entity: dict[str, Any]) -> str | None:
    release = entity.get("releaseDate")
    if isinstance(release, dict) and release.get("isoString"):
        return str(release["isoString"])[:10]
    return None


def _first(meta: dict[str, list[str]], key: str) -> str | None:
    values = meta.get(key)
    return values[0] if values else None


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def album_from_description(description: str | None) -> str | None:
    """``"Artist · Album · Song · 1987"`` -> ``"Album"``."""
    if not description:
        return None
    parts = description.split(" · ")
    if len(parts) < 4:
        return None
    return " · ".join(parts[1:-2]).strip() or None


def track_from_list_item(item: dict[str, Any]) -> Track | None:
    """Map one ``trackList`` entry (album / playlist / artist embeds)."""
    if item.get("entityType", "track") != "track":
        return None
    spotify_id = _id_from_uri(item.get("uri"))
    title = item.get("title")
    if not spotify_id or not title:
        return None
    return Track(
        spotify_id=spotify_id,
        title=title,
        artists=split_artists(item.get("subtitle")),
        duration_ms=int(item.get("duration") or 0),
        explicit=bool(item.get("isExplicit")),
    )


class SpotifyEmbedProvider:
    name = "spotify-embed"

    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self._album_cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _fetch(self, url: str) -> str:
        try:
            response = self.http.get(
                url, headers={"User-Agent": BROWSER_USER_AGENT, "Accept-Language": "en"}
            )
        except NetworkError as exc:
            raise MetadataError(f"Could not reach Spotify: {exc}") from exc
        if response.status_code == 404:
            raise NotFoundError(f"Spotify page not found: {url}")
        if not response.is_success:
            raise MetadataError(f"Spotify page {url} returned HTTP {response.status_code}")
        # Spotify's pages are UTF-8 but don't always say so; don't let httpx guess.
        return response.content.decode("utf-8", errors="replace")

    def _embed(self, kind: str, item_id: str) -> dict[str, Any]:
        return parse_entity(self._fetch(EMBED_URL.format(kind=kind, id=item_id)))

    def _page_meta(self, kind: str, item_id: str) -> dict[str, list[str]]:
        return parse_meta(self._fetch(PAGE_URL.format(kind=kind, id=item_id)))

    def _album_info(self, album_id: str) -> dict[str, Any]:
        """Album-level facts, cached: name, artists, track count, cover, release date."""
        with self._lock:
            cached = self._album_cache.get(album_id)
        if cached is not None:
            return cached
        entity = self._embed("album", album_id)
        info: dict[str, Any] = {
            "name": entity.get("name") or entity.get("title"),
            "artists": split_artists(entity.get("subtitle")),
            "total": len(entity.get("trackList") or []) or None,
            "cover": _largest_image(entity),
            "release_date": _release_date(entity),
            "tracks": entity.get("trackList") or [],
        }
        if not info["release_date"]:
            try:
                info["release_date"] = _first(
                    self._page_meta("album", album_id), "music:release_date"
                )
            except MetadataError as exc:
                log.debug("album page for %s: %s", album_id, exc)
        with self._lock:
            self._album_cache[album_id] = info
        return info

    # -- MetadataProvider
    def get_track(self, track_id: str) -> Track:
        entity = self._embed("track", track_id)
        track = Track(
            spotify_id=track_id,
            title=entity.get("name") or entity.get("title") or track_id,
            artists=[a["name"] for a in entity.get("artists") or [] if a.get("name")],
            duration_ms=int(entity.get("duration") or 0),
            release_date=_release_date(entity),
            explicit=bool(entity.get("isExplicit")),
            cover_url=_largest_image(entity),
        )
        return self._add_album_details(track)

    def _add_album_details(self, track: Track) -> Track:
        """Use the public track page's meta tags (+ the album embed) for album fields."""
        assert track.spotify_id
        try:
            meta = self._page_meta("track", track.spotify_id)
        except MetadataError as exc:
            log.debug("track page for %s: %s", track.spotify_id, exc)
            return track.model_copy(
                update={"album_artist": track.album_artist or track.primary_artist}
            )
        updates: dict[str, Any] = {}
        album_url = _first(meta, "music:album")
        album_id_match = _SPOTIFY_ID_IN_URL.search(album_url or "")
        album_id = album_id_match.group(1) if album_id_match else None
        updates["album"] = track.album or album_from_description(_first(meta, "og:description"))
        updates["album_id"] = track.album_id or album_id
        updates["track_number"] = track.track_number or _int(_first(meta, "music:album:track"))
        updates["release_date"] = _first(meta, "music:release_date") or track.release_date
        updates["cover_url"] = track.cover_url or _first(meta, "og:image")
        if not track.duration_ms:
            seconds = _int(_first(meta, "music:duration"))
            updates["duration_ms"] = seconds * 1000 if seconds else 0
        if album_id:
            try:
                album = self._album_info(album_id)
            except MetadataError as exc:
                log.debug("album embed for %s: %s", album_id, exc)
            else:
                updates["album"] = album["name"] or updates["album"]
                updates["album_artist"] = (album["artists"] or [None])[0]
                updates["total_tracks"] = album["total"]
                updates["cover_url"] = album["cover"] or updates["cover_url"]
                if not updates["track_number"]:
                    ids = [_id_from_uri(t.get("uri")) for t in album["tracks"]]
                    if track.spotify_id in ids:
                        updates["track_number"] = ids.index(track.spotify_id) + 1
        if not updates.get("album_artist"):
            updates["album_artist"] = track.album_artist or track.primary_artist
        return track.model_copy(update={k: v for k, v in updates.items() if v is not None})

    def get_album(self, album_id: str) -> Collection:
        info = self._album_info(album_id)
        tracks: list[Track] = []
        for number, item in enumerate(info["tracks"], start=1):
            track = track_from_list_item(item)
            if track is None:
                continue
            tracks.append(
                track.model_copy(
                    update={
                        "album": info["name"],
                        "album_id": album_id,
                        "album_artist": (info["artists"] or track.artists or [None])[0],
                        "track_number": number,
                        "total_tracks": info["total"],
                        "release_date": info["release_date"],
                        "cover_url": info["cover"],
                    }
                )
            )
        return Collection(
            kind="album",
            id=album_id,
            name=info["name"] or album_id,
            owner=", ".join(info["artists"]) or None,
            tracks=tracks,
            total=info["total"],
            source=self.name,
        )

    def _list_collection(self, kind: str, item_id: str) -> tuple[dict[str, Any], list[Track], int]:
        entity = self._embed(kind, item_id)
        tracks: list[Track] = []
        skipped = 0
        for item in entity.get("trackList") or []:
            track = track_from_list_item(item)
            if track is None:
                skipped += 1
            else:
                tracks.append(track)
        return entity, tracks, skipped

    def get_playlist(self, playlist_id: str) -> Collection:
        entity, tracks, skipped = self._list_collection("playlist", playlist_id)
        warnings: list[str] = []
        total: int | None = len(tracks) + skipped
        if total is not None and total >= EMBED_PLAYLIST_LIMIT:
            try:
                count = _int(_first(self._page_meta("playlist", playlist_id), "music:song_count"))
            except MetadataError:
                count = None
            total = count or total
            if count is None or count > len(tracks) + skipped:
                warnings.append(
                    f"Without Spotify API credentials only the first {EMBED_PLAYLIST_LIMIT} "
                    f"tracks of this playlist are visible"
                    + (f" (it has {count})." if count else ".")
                    + f" Spotify developer credentials show all of them ({CREDENTIALS_HELP})."
                )
        if skipped:
            warnings.append(f"Skipped {skipped} episode(s) or unavailable item(s).")
        return Collection(
            kind="playlist",
            id=playlist_id,
            name=entity.get("name") or entity.get("title") or playlist_id,
            owner=(entity.get("subtitle") or "").strip() or None,
            tracks=tracks,
            total=total,
            skipped=skipped,
            warnings=warnings,
            source=self.name,
        )

    def get_artist(self, artist_id: str) -> Collection:
        entity, tracks, skipped = self._list_collection("artist", artist_id)
        return Collection(
            kind="artist",
            id=artist_id,
            name=entity.get("name") or entity.get("title") or artist_id,
            tracks=tracks,
            skipped=skipped,
            warnings=[
                "Without Spotify API credentials only an artist's top tracks are visible, "
                "not their full discography."
            ],
            source=self.name,
        )

    def get_liked(self) -> Collection:
        raise RestrictedError(
            "Liked Songs are private: they need Spotify developer credentials and a Spotify "
            f"login ({CREDENTIALS_HELP})."
        )

    def enrich(self, track: Track) -> Track:
        if not track.spotify_id or not track.is_incomplete:
            return track
        try:
            entity = self._embed("track", track.spotify_id)
        except MetadataError as exc:
            log.debug("track embed for %s: %s", track.spotify_id, exc)
        else:
            artists = [a["name"] for a in entity.get("artists") or [] if a.get("name")]
            track = track.model_copy(
                update={
                    "artists": artists or track.artists,
                    "release_date": track.release_date or _release_date(entity),
                    "cover_url": track.cover_url or _largest_image(entity),
                    "explicit": track.explicit or bool(entity.get("isExplicit")),
                }
            )
        return self._add_album_details(track)
