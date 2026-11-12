"""Round-trip tagging on real ffmpeg-generated files + LRCLIB client."""

import shutil
from pathlib import Path

import mutagen
import pytest
import respx
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus

from tests.conftest import requires_ffmpeg
from tests.fakes import make_track
from tunegrab.models import Track
from tunegrab.net import HttpClient
from tunegrab.tagging.lyrics import LRCLIB_API, Lyrics, LyricsClient, strip_timestamps, write_lrc
from tunegrab.tagging.tagger import TaggingError, image_mime, tag_file, to_embeddable_image

TRACK = make_track(
    3,
    title="Café Song",
    artists=["Main Artist", "Guest"],
    album="The Album",
    album_artist="Main Artist",
    total_tracks=12,
    disc_number=1,
    release_date="2001-02-03",
    genres=["rock", "pop"],
    isrc="USABC0100001",
    explicit=True,
)
LYRICS = "line one\nline two"


def copy(src: Path, tmp_path: Path) -> Path:
    dest = tmp_path / src.name
    shutil.copy(src, dest)
    return dest


@requires_ffmpeg
def test_mp3_roundtrip(audio_files: dict[str, Path], jpeg_bytes: bytes, tmp_path: Path) -> None:
    path = copy(audio_files["mp3"], tmp_path)
    tag_file(path, TRACK, jpeg_bytes, LYRICS)
    tags = ID3(path)
    assert tags.version[1] == 4
    assert tags["TIT2"].text == ["Café Song"]
    assert tags["TPE1"].text == ["Main Artist", "Guest"]
    assert tags["TALB"].text == ["The Album"] and tags["TPE2"].text == ["Main Artist"]
    assert tags["TRCK"].text == ["3/12"] and tags["TPOS"].text == ["1"]
    assert str(tags["TDRC"].text[0]) == "2001-02-03"
    assert tags["TCON"].text == ["rock", "pop"] and tags["TSRC"].text == ["USABC0100001"]
    assert tags["COMM::eng"].text == [TRACK.spotify_url]
    assert tags["TXXX:ITUNESADVISORY"].text == ["1"]
    assert tags["USLT::eng"].text == LYRICS
    apic = tags.getall("APIC")[0]
    assert apic.data == jpeg_bytes and apic.mime == "image/jpeg" and apic.type == 3
    assert mutagen.File(path).info.length > 0.5  # still playable audio


@requires_ffmpeg
def test_m4a_roundtrip(audio_files: dict[str, Path], jpeg_bytes: bytes, tmp_path: Path) -> None:
    path = copy(audio_files["m4a"], tmp_path)
    tag_file(path, TRACK, jpeg_bytes, LYRICS)
    tags = MP4(path).tags
    assert tags is not None
    assert tags["\xa9nam"] == ["Café Song"] and tags["\xa9ART"] == ["Main Artist", "Guest"]
    assert tags["\xa9alb"] == ["The Album"] and tags["aART"] == ["Main Artist"]
    assert tags["trkn"] == [(3, 12)] and tags["disk"] == [(1, 0)]
    assert tags["\xa9day"] == ["2001-02-03"] and tags["\xa9gen"] == ["rock", "pop"]
    assert bytes(tags["----:com.apple.iTunes:ISRC"][0]) == b"USABC0100001"
    assert tags["rtng"] == [1] and tags["\xa9lyr"] == [LYRICS]
    assert bytes(tags["covr"][0]) == jpeg_bytes


@requires_ffmpeg
@pytest.mark.parametrize("ext", ["opus", "flac"])
def test_vorbis_roundtrip(
    ext: str, audio_files: dict[str, Path], jpeg_bytes: bytes, tmp_path: Path
) -> None:
    path = copy(audio_files[ext], tmp_path)
    tag_file(path, TRACK, jpeg_bytes, LYRICS)
    audio = OggOpus(path) if ext == "opus" else FLAC(path)
    tags = audio.tags
    assert tags is not None
    assert tags["title"] == ["Café Song"] and tags["artist"] == ["Main Artist", "Guest"]
    assert tags["album"] == ["The Album"] and tags["albumartist"] == ["Main Artist"]
    assert tags["tracknumber"] == ["3"] and tags["tracktotal"] == ["12"]
    assert tags["date"] == ["2001-02-03"] and tags["genre"] == ["rock", "pop"]
    assert tags["isrc"] == ["USABC0100001"] and tags["lyrics"] == [LYRICS]
    if ext == "flac":
        assert audio.pictures[0].data == jpeg_bytes  # type: ignore[union-attr]
    else:
        assert tags["metadata_block_picture"]


@requires_ffmpeg
def test_retag_replaces_old_tags(audio_files: dict[str, Path], tmp_path: Path) -> None:
    path = copy(audio_files["mp3"], tmp_path)
    tag_file(path, TRACK, None, LYRICS)
    tag_file(path, Track(title="New", artists=["X"]))
    tags = ID3(path)
    assert tags["TIT2"].text == ["New"] and "TALB" not in tags and not tags.getall("USLT")


def test_unknown_extension(tmp_path: Path) -> None:
    with pytest.raises(TaggingError):
        tag_file(tmp_path / "a.wav", TRACK)


@requires_ffmpeg
def test_corrupt_file_raises_tagging_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.m4a"
    bad.write_bytes(b"not audio")
    with pytest.raises(TaggingError):
        tag_file(bad, TRACK)


@requires_ffmpeg
def test_images(jpeg_bytes: bytes, tmp_path: Path) -> None:
    import subprocess

    from tests.conftest import FFMPEG

    assert image_mime(jpeg_bytes) == "image/jpeg"
    assert image_mime(b"\x89PNG\r\n\x1a\nxxxx") == "image/png"
    assert to_embeddable_image(None) is None
    webp = tmp_path / "c.webp"
    subprocess.run(
        [
            FFMPEG or "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=32x32",
            "-frames:v",
            "1",
            str(webp),
        ],
        check=True,
    )
    converted = to_embeddable_image(webp.read_bytes())
    assert converted is not None and converted[1] == "image/jpeg"
    assert to_embeddable_image(b"garbage") is None


# --- lyrics -----------------------------------------------------------------------


def test_lyrics_text() -> None:
    assert strip_timestamps("[00:01.00] a\n[00:02.50][00:03.00]b") == "a\nb"
    assert Lyrics(plain="p", synced="[00:01.00]s").text == "p"
    assert Lyrics(plain=None, synced="[00:01.00]s").text == "s"
    assert Lyrics(plain="[00:00.87] stamped", synced=None).text == "stamped"
    assert Lyrics(plain=None, synced=None, instrumental=True).text is None


@respx.mock
def test_lrclib_exact() -> None:
    route = respx.get(f"{LRCLIB_API}/get").respond(
        200, json={"plainLyrics": "hi", "syncedLyrics": "[00:01.00]hi"}
    )
    found = LyricsClient(HttpClient(retries=0)).fetch(TRACK)
    assert found == Lyrics(plain="hi", synced="[00:01.00]hi")
    params = route.calls[0].request.url.params
    assert params["track_name"] == "Café Song" and params["artist_name"] == "Main Artist"
    assert params["album_name"] == "The Album" and params["duration"] == "200"


@respx.mock
def test_lrclib_search_fallback_filters_duration() -> None:
    respx.get(f"{LRCLIB_API}/get").respond(404)
    respx.get(f"{LRCLIB_API}/search").respond(
        200,
        json=[
            {"duration": 400, "plainLyrics": "wrong length"},
            {"duration": 201, "plainLyrics": "plain only"},
            {"duration": 199, "plainLyrics": "p", "syncedLyrics": "[00:01.00]synced"},
        ],
    )
    found = LyricsClient(HttpClient(retries=0)).fetch(TRACK)
    assert found is not None and found.synced == "[00:01.00]synced"


@respx.mock
def test_lrclib_nothing() -> None:
    respx.get(f"{LRCLIB_API}/get").respond(404)
    respx.get(f"{LRCLIB_API}/search").respond(200, json=[])
    assert LyricsClient(HttpClient(retries=0)).fetch(TRACK) is None
    respx.get(f"{LRCLIB_API}/search").respond(500)
    assert LyricsClient(HttpClient(retries=0)).fetch(TRACK) is None


@respx.mock
def test_lrclib_skips_get_without_album() -> None:
    get = respx.get(f"{LRCLIB_API}/get").respond(200, json={"plainLyrics": "x"})
    respx.get(f"{LRCLIB_API}/search").respond(200, json=[])
    LyricsClient(HttpClient(retries=0)).fetch(make_track(1, album=None))
    assert not get.called


def test_write_lrc(tmp_path: Path) -> None:
    path = write_lrc(tmp_path / "song.mp3", "[00:01.00]hi\n")
    assert path.name == "song.lrc" and path.read_text("utf-8") == "[00:01.00]hi\n"
