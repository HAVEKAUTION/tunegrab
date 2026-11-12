"""ffmpeg post-processing (via yt-dlp's FFmpegExtractAudio) to the chosen format.

YouTube audio is lossy (~128-160 kbps Opus/AAC). ``m4a`` and ``opus`` keep the
original stream without re-encoding (most faithful); ``mp3`` re-encodes, and ``flac``
only wraps lossy audio in a lossless container: neither adds quality.
"""

from __future__ import annotations

from typing import Any

from tunegrab.config import AudioFormat

FORMAT_HELP = (
    "mp3 (re-encoded, plays everywhere), m4a (original AAC, no re-encode), "
    "opus (original Opus, no re-encode, best fidelity), flac (lossless container, "
    "no quality gain). YouTube audio is ~128-160 kbps lossy whatever you pick."
)

# "-drc" formats are YouTube's "stable volume" copies: dynamic range compressed, so worse
# for music (yt-dlp#8259). They are only used when nothing else exists.
NO_DRC = "[format_id!$=-drc]"
FORMAT_SELECTORS: dict[str, str] = {
    "mp3": f"bestaudio{NO_DRC}/bestaudio/best",
    "m4a": (
        f"bestaudio[ext=m4a]{NO_DRC}/bestaudio[acodec^=mp4a]{NO_DRC}/"
        f"bestaudio{NO_DRC}/bestaudio/best"
    ),
    "opus": f"bestaudio[acodec=opus]{NO_DRC}/bestaudio{NO_DRC}/bestaudio/best",
    "flac": f"bestaudio{NO_DRC}/bestaudio/best",
}


def format_selector(fmt: AudioFormat) -> str:
    return FORMAT_SELECTORS[fmt]


def postprocessors(fmt: AudioFormat, bitrate: int) -> list[dict[str, Any]]:
    """yt-dlp post-processor config. When the source codec already matches (AAC for
    m4a, Opus for opus) FFmpegExtractAudio copies the stream instead of re-encoding."""
    extract: dict[str, Any] = {"key": "FFmpegExtractAudio", "preferredcodec": fmt}
    if fmt == "mp3":
        extract["preferredquality"] = str(bitrate)
    return [extract]


def extension(fmt: AudioFormat) -> str:
    return fmt
