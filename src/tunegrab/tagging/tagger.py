"""mutagen tag writing (ID3v2.4 / MP4 / Vorbis comments) and cover art embedding."""

from __future__ import annotations

import base64
import logging
import shutil
import subprocess
from pathlib import Path

import mutagen
from mutagen.flac import FLAC, Picture, VCFLACDict
from mutagen.id3 import (
    APIC,
    COMM,
    ID3,
    TALB,
    TCON,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
    TSRC,
    TXXX,
    USLT,
)
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from tunegrab.models import Track

log = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = (".mp3", ".m4a", ".opus", ".ogg", ".flac")


class TaggingError(Exception):
    pass


def image_mime(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return None


def to_embeddable_image(data: bytes | None) -> tuple[bytes, str] | None:
    """Return (bytes, mime) for JPEG/PNG; other formats (e.g. WebP) are converted to
    JPEG with ffmpeg when available, otherwise dropped."""
    if not data:
        return None
    mime = image_mime(data)
    if mime:
        return data, mime
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        proc = subprocess.run(
            [ffmpeg, "-loglevel", "error", "-i", "pipe:0", "-f", "mjpeg", "-q:v", "2", "pipe:1"],
            input=data,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0 and image_mime(proc.stdout) == "image/jpeg":
        return proc.stdout, "image/jpeg"
    return None


def _number(value: int | None, total: int | None) -> str | None:
    if not value:
        return None
    return f"{value}/{total}" if total else str(value)


def _tag_mp3(path: Path, track: Track, cover: tuple[bytes, str] | None, lyrics: str | None) -> None:
    tags = ID3()
    tags.add(TIT2(encoding=3, text=track.title))
    if track.artists:
        tags.add(TPE1(encoding=3, text=track.artists))
    if track.album:
        tags.add(TALB(encoding=3, text=track.album))
    if track.album_artist:
        tags.add(TPE2(encoding=3, text=track.album_artist))
    if number := _number(track.track_number, track.total_tracks):
        tags.add(TRCK(encoding=3, text=number))
    if disc := _number(track.disc_number, None):
        tags.add(TPOS(encoding=3, text=disc))
    if track.release_date:
        tags.add(TDRC(encoding=3, text=track.release_date))
    if track.genres:
        tags.add(TCON(encoding=3, text=track.genres))
    if track.isrc:
        tags.add(TSRC(encoding=3, text=track.isrc))
    if track.spotify_url:
        tags.add(COMM(encoding=3, lang="eng", desc="", text=track.spotify_url))
        tags.add(TXXX(encoding=3, desc="SPOTIFY_URL", text=track.spotify_url))
    if track.explicit:
        tags.add(TXXX(encoding=3, desc="ITUNESADVISORY", text="1"))
    if lyrics:
        tags.add(USLT(encoding=3, lang="eng", desc="", text=lyrics))
    if cover:
        tags.add(APIC(encoding=3, mime=cover[1], type=3, desc="Cover", data=cover[0]))
    tags.save(path, v2_version=4)


def _tag_mp4(path: Path, track: Track, cover: tuple[bytes, str] | None, lyrics: str | None) -> None:
    audio = MP4(path)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    assert tags is not None
    tags.clear()
    tags["\xa9nam"] = [track.title]
    if track.artists:
        tags["\xa9ART"] = track.artists
    if track.album:
        tags["\xa9alb"] = [track.album]
    if track.album_artist:
        tags["aART"] = [track.album_artist]
    if track.track_number:
        tags["trkn"] = [(track.track_number, track.total_tracks or 0)]
    if track.disc_number:
        tags["disk"] = [(track.disc_number, 0)]
    if track.release_date:
        tags["\xa9day"] = [track.release_date]
    if track.genres:
        tags["\xa9gen"] = track.genres
    if track.isrc:
        tags["----:com.apple.iTunes:ISRC"] = [MP4FreeForm(track.isrc.encode())]
    if track.spotify_url:
        tags["\xa9cmt"] = [track.spotify_url]
    tags["rtng"] = [1 if track.explicit else 0]
    if lyrics:
        tags["\xa9lyr"] = [lyrics]
    if cover:
        fmt = MP4Cover.FORMAT_PNG if cover[1] == "image/png" else MP4Cover.FORMAT_JPEG
        tags["covr"] = [MP4Cover(cover[0], imageformat=fmt)]
    audio.save()


def vorbis_fields(track: Track, lyrics: str | None) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {"title": [track.title]}
    if track.artists:
        fields["artist"] = list(track.artists)
    if track.album:
        fields["album"] = [track.album]
    if track.album_artist:
        fields["albumartist"] = [track.album_artist]
    if track.track_number:
        fields["tracknumber"] = [str(track.track_number)]
    if track.total_tracks:
        fields["tracktotal"] = [str(track.total_tracks)]
        fields["totaltracks"] = [str(track.total_tracks)]
    if track.disc_number:
        fields["discnumber"] = [str(track.disc_number)]
    if track.release_date:
        fields["date"] = [track.release_date]
    if track.genres:
        fields["genre"] = list(track.genres)
    if track.isrc:
        fields["isrc"] = [track.isrc]
    if track.spotify_url:
        fields["comment"] = [track.spotify_url]
        fields["spotify_url"] = [track.spotify_url]
    if track.explicit:
        fields["itunesadvisory"] = ["1"]
    if lyrics:
        fields["lyrics"] = [lyrics]
    return fields


def _picture(cover: tuple[bytes, str]) -> Picture:
    picture = Picture()
    picture.type = 3
    picture.mime = cover[1]
    picture.desc = "Cover"
    picture.data = cover[0]
    return picture


def _tag_ogg(path: Path, track: Track, cover: tuple[bytes, str] | None, lyrics: str | None) -> None:
    audio: OggOpus | OggVorbis
    try:
        audio = OggOpus(path)
    except Exception:
        audio = OggVorbis(path)
    assert audio.tags is not None
    audio.tags.clear()
    for key, values in vorbis_fields(track, lyrics).items():
        audio.tags[key] = values
    if cover:
        encoded = base64.b64encode(_picture(cover).write()).decode("ascii")
        audio.tags["metadata_block_picture"] = [encoded]
    audio.save()


def _tag_flac(
    path: Path, track: Track, cover: tuple[bytes, str] | None, lyrics: str | None
) -> None:
    audio = FLAC(path)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    assert isinstance(tags, VCFLACDict)
    tags.clear()
    for key, values in vorbis_fields(track, lyrics).items():
        tags[key] = values
    audio.clear_pictures()
    if cover:
        audio.add_picture(_picture(cover))
    audio.save()


def tag_file(
    path: Path, track: Track, cover: bytes | None = None, lyrics: str | None = None
) -> None:
    """Write all tags (replacing existing ones) to ``path`` based on its extension."""
    image = to_embeddable_image(cover)
    suffix = path.suffix.lower()
    writers = {
        ".mp3": _tag_mp3,
        ".m4a": _tag_mp4,
        ".opus": _tag_ogg,
        ".ogg": _tag_ogg,
        ".flac": _tag_flac,
    }
    writer = writers.get(suffix)
    if writer is None:
        raise TaggingError(f"Don't know how to tag {suffix} files")
    try:
        writer(path, track, image, lyrics)
    except TaggingError:
        raise
    except Exception as exc:
        raise TaggingError(f"Could not tag {path.name}: {exc}") from exc


def read_title_artist(path: Path) -> tuple[str, str] | None:
    """(title, artist) from a file's tags, or None if it has none or can't be read."""
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:
        return None
    if audio is None or not audio.tags:
        return None
    title = audio.tags.get("title") or [""]
    artist = audio.tags.get("artist") or [""]
    return (str(title[0]), str(artist[0])) if title[0] else None


class MutagenTagger:
    def tag(
        self, path: Path, track: Track, cover: bytes | None = None, lyrics: str | None = None
    ) -> None:
        tag_file(path, track, cover, lyrics)

    def identify(self, path: Path) -> tuple[str, str] | None:
        return read_title_artist(path)
