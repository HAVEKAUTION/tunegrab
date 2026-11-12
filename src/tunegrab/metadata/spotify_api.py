"""Official Spotify Web API provider (client credentials + optional PKCE user login).

Spotify is used as a *metadata source only*. Development Mode apps (since Feb 2026)
need a Premium owner and see fewer endpoints/fields, so every call maps 403/404 to a
typed error that lets the resolver fall back to the embed provider.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import secrets
import threading
import time
import webbrowser
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from rapidfuzz import fuzz

from tunegrab.config import Settings, keyring_get, keyring_set
from tunegrab.metadata.base import MetadataError, NotFoundError, RestrictedError
from tunegrab.models import Collection, Track
from tunegrab.net import HttpClient, NetworkError

log = logging.getLogger(__name__)

API_BASE = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
SCOPES = "playlist-read-private playlist-read-collaborative user-library-read"
USER_TOKEN_KEY = "spotify_user_token"
_EXPIRY_MARGIN_S = 60


# --- token storage -------------------------------------------------------------


class TokenStore:
    """Persists the user's OAuth token JSON in the OS keyring."""

    def load(self) -> dict[str, Any] | None:
        raw = keyring_get(USER_TOKEN_KEY)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def save(self, token: dict[str, Any]) -> None:
        keyring_set(USER_TOKEN_KEY, json.dumps(token))

    def clear(self) -> None:
        keyring_set(USER_TOKEN_KEY, None)


class MemoryTokenStore(TokenStore):
    def __init__(self, token: dict[str, Any] | None = None) -> None:
        self.token = token

    def load(self) -> dict[str, Any] | None:
        return self.token

    def save(self, token: dict[str, Any]) -> None:
        self.token = token

    def clear(self) -> None:
        self.token = None


def _token_with_expiry(payload: dict[str, Any], now: float) -> dict[str, Any]:
    token = dict(payload)
    token["expires_at"] = now + float(payload.get("expires_in", 3600))
    return token


class SpotifyAuth:
    """Hands out a valid bearer token: the user token if logged in, else client credentials."""

    def __init__(
        self,
        client_id: str,
        client_secret: str | None,
        http: HttpClient,
        store: TokenStore | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.http = http
        self.store = store or TokenStore()
        self.clock = clock
        self._app_token: dict[str, Any] | None = None
        self._user_token: dict[str, Any] | None = None
        self._user_loaded = False
        self._lock = threading.Lock()

    @property
    def has_user(self) -> bool:
        with self._lock:
            return self._load_user() is not None

    def _load_user(self) -> dict[str, Any] | None:
        if not self._user_loaded:
            token = self.store.load()
            owner = token.get("client_id") if token else None
            if owner and owner != self.client_id:
                # Logged in with another Spotify app; its token (and refresh token) would
                # keep acting as that app. Ignore it until `tunegrab login` is run again.
                log.warning(
                    "Ignoring the saved Spotify login: it belongs to a different client ID. "
                    "Run `tunegrab login` again."
                )
                token = None
            self._user_token = token
            self._user_loaded = True
        return self._user_token

    def _fresh(self, token: dict[str, Any] | None) -> bool:
        return bool(token) and float(token.get("expires_at", 0)) - _EXPIRY_MARGIN_S > self.clock()  # type: ignore[union-attr]

    def access_token(self) -> str:
        with self._lock:
            user = self._load_user()
            if user is not None:
                if not self._fresh(user):
                    user = self._refresh_user(user)
                if user is not None:
                    return str(user["access_token"])
            if not self._fresh(self._app_token):
                self._app_token = self._client_credentials()
            assert self._app_token is not None
            return str(self._app_token["access_token"])

    def invalidate(self) -> None:
        """Forget cached tokens after a 401 so the next call fetches fresh ones."""
        with self._lock:
            self._app_token = None
            if self._user_token is not None:
                self._user_token["expires_at"] = 0

    def _client_credentials(self) -> dict[str, Any]:
        if not self.client_secret:
            raise RestrictedError("Spotify client secret missing; cannot use the Web API.")
        try:
            response = self.http.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(self.client_id, self.client_secret),
            )
        except NetworkError as exc:
            raise MetadataError(f"Could not reach Spotify: {exc}") from exc
        if response.status_code in (400, 401):
            raise RestrictedError(
                "Spotify rejected the client ID/secret. Check SPOTIFY_CLIENT_ID and "
                "SPOTIFY_CLIENT_SECRET (docs/SETUP.md)."
            )
        if not response.is_success:
            raise MetadataError(f"Spotify token request failed: HTTP {response.status_code}")
        return _token_with_expiry(response.json(), self.clock())

    def _refresh_user(self, token: dict[str, Any]) -> dict[str, Any] | None:
        refresh = token.get("refresh_token")
        if not refresh:
            self._user_token = None
            return None
        try:
            response = self.http.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "client_id": self.client_id,
                },
            )
        except NetworkError as exc:
            raise MetadataError(f"Could not reach Spotify: {exc}") from exc
        if not response.is_success:
            log.warning(
                "Spotify user token refresh failed (HTTP %s); run `tunegrab login`",
                response.status_code,
            )
            self._user_token = None
            return None
        new = _token_with_expiry(response.json(), self.clock())
        new.setdefault("refresh_token", refresh)
        new["client_id"] = self.client_id
        self._user_token = new
        try:
            self.store.save(new)
        except RuntimeError as exc:
            log.warning("Could not persist refreshed token: %s", exc)
        return new


# --- PKCE login -------------------------------------------------------------------


def make_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) per RFC 7636 (S256)."""
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "state": state,
            "scope": SCOPES,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def wait_for_callback(redirect_uri: str, state: str, timeout: float = 300) -> str:
    """Run a one-shot loopback HTTP server and return the ``code`` from the redirect."""
    parts = urlsplit(redirect_uri)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 80
    expected_path = parts.path or "/"
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urlsplit(self.path)
            if url.path != expected_path:
                self.send_response(404)
                self.end_headers()
                return
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if query.get("state") != state:
                result["error"] = "state mismatch (possible CSRF); try again"
            elif "error" in query:
                result["error"] = query["error"]
            elif "code" in query:
                result["code"] = query["code"]
            else:
                result["error"] = "no code in callback"
            ok = "code" in result
            body = (
                "<h2>TuneGrab is now logged in to Spotify.</h2>You can close this tab."
                if ok
                else f"<h2>Login failed</h2>{result.get('error', '')}"
            ).encode()
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = http.server.HTTPServer((host, port), Handler)
    server.timeout = 1
    deadline = time.monotonic() + timeout
    try:
        while not result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "code" in result:
        return result["code"]
    raise MetadataError(f"Spotify login failed: {result.get('error', 'timed out')}")


def exchange_code(
    http: HttpClient, client_id: str, code: str, verifier: str, redirect_uri: str
) -> dict[str, Any]:
    response = http.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    if not response.is_success:
        raise MetadataError(f"Spotify token exchange failed: HTTP {response.status_code}")
    return _token_with_expiry(response.json(), time.time())


def login(
    settings: Settings,
    http: HttpClient,
    store: TokenStore | None = None,
    open_browser: Callable[[str], Any] = webbrowser.open,
    on_url: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Interactive Authorization Code + PKCE login; stores the token in the keyring."""
    if not settings.spotify_client_id:
        raise RestrictedError("Set SPOTIFY_CLIENT_ID first (docs/SETUP.md).")
    verifier, challenge = make_pkce_pair()
    state = secrets.token_urlsafe(16)
    url = build_authorize_url(
        settings.spotify_client_id, settings.spotify_redirect_uri, challenge, state
    )
    if on_url:
        on_url(url)
    open_browser(url)
    code = wait_for_callback(settings.spotify_redirect_uri, state)
    token = exchange_code(
        http, settings.spotify_client_id, code, verifier, settings.spotify_redirect_uri
    )
    token["client_id"] = settings.spotify_client_id  # see SpotifyAuth._load_user
    (store or TokenStore()).save(token)
    return token


# --- mapping ---------------------------------------------------------------------


def _largest_image(images: list[dict[str, Any]] | None) -> str | None:
    if not images:
        return None
    best = max(images, key=lambda img: (img.get("width") or 0) * (img.get("height") or 0))
    url = best.get("url")
    return str(url) if url else None


def track_from_api(item: dict[str, Any], album: dict[str, Any] | None = None) -> Track | None:
    """Map a Spotify track object (full or simplified) to :class:`Track`.

    Returns ``None`` for things that aren't downloadable tracks (local files,
    episodes, removed items).
    """
    if not item or item.get("is_local") or item.get("type", "track") != "track":
        return None
    if not item.get("id") or not item.get("name"):
        return None
    alb: dict[str, Any] = album or item.get("album") or {}
    album_artists = [a["name"] for a in alb.get("artists") or [] if a.get("name")]
    return Track(
        spotify_id=item["id"],
        title=item["name"],
        artists=[a["name"] for a in item.get("artists") or [] if a.get("name")],
        album=alb.get("name"),
        album_artist=album_artists[0] if album_artists else None,
        album_id=alb.get("id"),
        track_number=item.get("track_number"),
        disc_number=item.get("disc_number"),
        total_tracks=alb.get("total_tracks"),
        release_date=alb.get("release_date") or None,
        duration_ms=int(item.get("duration_ms") or 0),
        isrc=(item.get("external_ids") or {}).get("isrc"),
        explicit=bool(item.get("explicit")),
        cover_url=_largest_image(alb.get("images")),
        genres=list(alb.get("genres") or []),
    )


def _dedupe_key(track: Track) -> tuple[str, int]:
    title = " ".join(track.title.lower().split())
    return title, round(track.duration_ms / 3000)


# --- provider ----------------------------------------------------------------------


class SpotifyAPIProvider:
    name = "spotify-api"

    def __init__(self, auth: SpotifyAuth, http: HttpClient) -> None:
        self.auth = auth
        self.http = http

    @classmethod
    def from_settings(cls, settings: Settings, http: HttpClient) -> SpotifyAPIProvider | None:
        if not settings.spotify_client_id:
            return None
        secret = (
            settings.spotify_client_secret.get_secret_value()
            if settings.spotify_client_secret
            else None
        )
        auth = SpotifyAuth(settings.spotify_client_id, secret, http)
        if not secret and not auth.has_user:
            return None
        return cls(auth, http)

    # -- low-level
    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = path if path.startswith("http") else API_BASE + path
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self.auth.access_token()}"}
            try:
                response = self.http.get(url, params=params, headers=headers)
            except NetworkError as exc:
                raise MetadataError(f"Could not reach Spotify: {exc}") from exc
            if response.status_code == 401 and attempt == 0:
                self.auth.invalidate()
                continue
            break
        status = response.status_code
        if status == 404:
            raise NotFoundError(
                f"Spotify API: not found ({path}). Spotify-owned editorial playlists are "
                "hidden from Development Mode apps."
            )
        if status in (401, 403):
            raise RestrictedError(
                f"Spotify API refused access to {path} (HTTP {status}). Development Mode apps "
                "need a Premium owner and only see some endpoints; private items need "
                "`tunegrab login`."
            )
        if status == 429:
            raise MetadataError("Spotify API rate limit hit; try again in a few minutes.")
        if not response.is_success:
            raise MetadataError(f"Spotify API error for {path}: HTTP {status}")
        return response.json()

    def _paginate(self, page: dict[str, Any] | None) -> Iterator[dict[str, Any]]:
        while page:
            yield from page.get("items") or []
            next_url = page.get("next")
            page = self._get(next_url) if next_url else None

    # -- MetadataProvider
    def get_track(self, track_id: str) -> Track:
        track = track_from_api(self._get(f"/tracks/{track_id}"))
        if track is None:
            raise NotFoundError(f"Spotify track {track_id} is not a downloadable track")
        return track

    def _album_tracks(self, album: dict[str, Any]) -> list[Track]:
        tracks: list[Track] = []
        for item in self._paginate(album.get("tracks")):
            track = track_from_api(item, album)
            if track is not None:
                tracks.append(track)
        return tracks

    def get_album(self, album_id: str) -> Collection:
        album = self._get(f"/albums/{album_id}")
        tracks = self._album_tracks(album)
        artists = [a["name"] for a in album.get("artists") or [] if a.get("name")]
        return Collection(
            kind="album",
            id=album_id,
            name=album.get("name") or album_id,
            owner=", ".join(artists) or None,
            tracks=tracks,
            total=album.get("total_tracks"),
            source=self.name,
        )

    def _playlist_items(self, playlist_id: str, playlist: dict[str, Any]) -> Iterator[Any]:
        # Feb 2026 renamed the playlist "tracks" field/endpoint to "items"; accept both.
        embedded = playlist.get("items") or playlist.get("tracks")
        if isinstance(embedded, dict) and "items" in embedded:
            yield from self._paginate(embedded)
            return
        try:
            first = self._get(f"/playlists/{playlist_id}/items")
        except NotFoundError:
            first = self._get(f"/playlists/{playlist_id}/tracks")
        yield from self._paginate(first)

    def get_playlist(self, playlist_id: str) -> Collection:
        playlist = self._get(f"/playlists/{playlist_id}")
        tracks: list[Track] = []
        skipped = 0
        for entry in self._playlist_items(playlist_id, playlist):
            item = entry.get("item") or entry.get("track") if isinstance(entry, dict) else None
            track = track_from_api(item) if isinstance(item, dict) else None
            if track is None:
                skipped += 1
            else:
                tracks.append(track)
        container = playlist.get("items") or playlist.get("tracks") or {}
        total = container.get("total") if isinstance(container, dict) else None
        warnings = (
            [f"Skipped {skipped} local file(s), episode(s) or unavailable item(s)."]
            if skipped
            else []
        )
        return Collection(
            kind="playlist",
            id=playlist_id,
            name=playlist.get("name") or playlist_id,
            owner=(playlist.get("owner") or {}).get("display_name"),
            tracks=tracks,
            total=total,
            skipped=skipped,
            warnings=warnings,
            source=self.name,
        )

    def get_artist(self, artist_id: str) -> Collection:
        artist = self._get(f"/artists/{artist_id}")
        artist_name = artist.get("name") or artist_id
        albums = list(
            self._paginate(
                self._get(f"/artists/{artist_id}/albums", params={"include_groups": "album,single"})
            )
        )
        # Full albums first (so a single's duplicate loses to the album version), oldest first.
        albums.sort(key=lambda a: (a.get("album_type") != "album", a.get("release_date") or ""))
        seen: set[tuple[str, int]] = set()
        seen_isrc: set[str] = set()
        tracks: list[Track] = []
        for summary in albums:
            album = self._get(f"/albums/{summary['id']}")
            for track in self._album_tracks(album):
                if not any(fuzz.ratio(a.lower(), artist_name.lower()) > 90 for a in track.artists):
                    continue
                key = _dedupe_key(track)
                if key in seen or (track.isrc and track.isrc in seen_isrc):
                    continue
                seen.add(key)
                if track.isrc:
                    seen_isrc.add(track.isrc)
                if artist.get("genres") and not track.genres:
                    track.genres = list(artist["genres"])
                tracks.append(track)
        return Collection(
            kind="artist", id=artist_id, name=artist_name, tracks=tracks, source=self.name
        )

    def get_liked(self) -> Collection:
        if not self.auth.has_user:
            raise RestrictedError("Liked Songs need a user login: run `tunegrab login`.")
        tracks = [
            t
            for entry in self._paginate(self._get("/me/tracks", params={"limit": 50}))
            if (t := track_from_api(entry.get("track") or {})) is not None
        ]
        return Collection(
            kind="liked", id="liked", name="Liked Songs", tracks=tracks, source=self.name
        )

    def enrich(self, track: Track) -> Track:
        if not track.is_incomplete or not track.spotify_id:
            return track
        return self.get_track(track.spotify_id)
