"""ffmpeg post-processing (via yt-dlp's FFmpegExtractAudio) to the chosen format.

YouTube audio is lossy (~128-160 kbps Opus/AAC). ``m4a`` and ``opus`` keep the
original stream without re-encoding (most faithful); ``mp3`` re-encodes, and ``flac``
only wraps lossy audio in a lossless container: neither adds quality.
"""

from __future__ import annotations

from typing import Any

from tunegrab.config import AudioFormat

FORMAT_HELP = (
    "m4a (default: original quality, no re-encode, plays everywhere modern), "
    "opus (original quality, smallest), mp3 (re-encoded, for old players), "
    "flac (re-encoded into a bigger file, no better). YouTube audio is ~128-160 kbps "
    "lossy whatever you pick."
)
VBR_V0 = "0"  # yt-dlp: a quality under 10 is VBR; 0 is LAME's best preset (V0)

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


def postprocessors(fmt: AudioFormat, bitrate: int | None) -> list[dict[str, Any]]:
    """yt-dlp post-processor config. When the source codec already matches (AAC for
    m4a, Opus for opus) FFmpegExtractAudio copies the stream instead of re-encoding.
    MP3 is VBR V0 unless a bitrate was chosen (then constant)."""
    extract: dict[str, Any] = {"key": "FFmpegExtractAudio", "preferredcodec": fmt}
    if fmt == "mp3":
        extract["preferredquality"] = str(bitrate) if bitrate else VBR_V0
    return [extract]


def extension(fmt: AudioFormat) -> str:
    return fmt
