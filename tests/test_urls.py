import pytest

from tunegrab.urls import (
    InvalidURLError,
    SpotifyRef,
    is_short_link,
    is_spotify_input,
    parse_spotify_url,
    parse_youtube_id,
)

TID = "4cOdK2wGLETKBW3PvgPWqT"


@pytest.mark.parametrize(
    ("text", "kind", "item"),
    [
        (f"https://open.spotify.com/track/{TID}", "track", TID),
        (f"https://open.spotify.com/track/{TID}?si=abc123&utm=x", "track", TID),
        (f"https://open.spotify.com/track/{TID}/", "track", TID),
        (f"  https://open.spotify.com/track/{TID}  ", "track", TID),
        (f"open.spotify.com/track/{TID}", "track", TID),
        (f"http://open.spotify.com/track/{TID}", "track", TID),
        (f"https://open.spotify.com/intl-de/track/{TID}", "track", TID),
        (f"https://open.spotify.com/intl-pt-BR/album/{TID}", "album", TID),
        (f"https://open.spotify.com/embed/playlist/{TID}", "playlist", TID),
        (f"https://open.spotify.com/user/someone/playlist/{TID}", "playlist", TID),
        (f"https://play.spotify.com/artist/{TID}", "artist", TID),
        (f"https://www.open.spotify.com/album/{TID}#frag", "album", TID),
        (f"spotify:track:{TID}", "track", TID),
        (f"spotify:album:{TID}", "album", TID),
        (f"spotify:user:bob:playlist:{TID}", "playlist", TID),
        (f"SPOTIFY:ARTIST:{TID}", "artist", TID),
        ("https://open.spotify.com/collection/tracks", "liked", ""),
        ("spotify:user:bob:collection", "liked", ""),
    ],
)
def test_parse_spotify(text: str, kind: str, item: str) -> None:
    assert parse_spotify_url(text) == SpotifyRef(kind, item)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "https://example.com/track/" + TID,
        "https://open.spotify.com/",
        "https://open.spotify.com/track/",
        "https://open.spotify.com/track/short",
        f"https://open.spotify.com/show/{TID}",
        f"https://open.spotify.com/episode/{TID}",
        "spotify:track:bad!id",
        "spotify:nothing",
        "Daft Punk - One More Time",
    ],
)
def test_parse_spotify_invalid(text: str) -> None:
    with pytest.raises(InvalidURLError):
        parse_spotify_url(text)


def test_ref_urls() -> None:
    assert SpotifyRef("album", TID).url == f"https://open.spotify.com/album/{TID}"
    assert SpotifyRef("track", TID).uri == f"spotify:track:{TID}"
    assert SpotifyRef("liked", "").url.endswith("/collection/tracks")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"https://open.spotify.com/track/{TID}", True),
        ("spotify:track:x", True),
        ("https://spotify.link/abc", True),
        ("Daft Punk - One More Time", False),
        ("https://youtube.com/watch?v=x", False),
    ],
)
def test_is_spotify_input(text: str, expected: bool) -> None:
    assert is_spotify_input(text) is expected


def test_short_links() -> None:
    assert is_short_link("https://spotify.link/AbC")
    assert is_short_link("spoti.fi/xyz")
    assert not is_short_link(f"https://open.spotify.com/track/{TID}")


@pytest.mark.parametrize(
    "text",
    [
        "dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/watch?feature=share&v=dQw4w9WgXcQ&t=10",
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=RD",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ?t=3",
        "youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ",
    ],
)
def test_parse_youtube(text: str) -> None:
    assert parse_youtube_id(text) == "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "https://www.youtube.com/watch",
        "https://vimeo.com/123",
        "https://youtube.com/@chan",
        "too-short",
    ],
)
def test_parse_youtube_invalid(text: str) -> None:
    with pytest.raises(InvalidURLError):
        parse_youtube_id(text)
