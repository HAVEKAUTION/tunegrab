"""yt-dlp used as a library: progress hooks, format selection, cancellation."""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from tunegrab.config import AudioFormat
from tunegrab.download.convert import extension, format_selector, postprocessors

log = logging.getLogger(__name__)

# (fraction 0..1, phase: "downloading" | "converting")
ProgressCallback = Callable[[float, str], None]

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class DownloadError(Exception):
    """yt-dlp could not fetch or convert the audio."""


class DownloadCancelled(DownloadError):
    """The user cancelled (Ctrl+C) while downloading."""


class Downloader(Protocol):
    def download(
        self,
        video_id: str,
        dest_dir: Path,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> Path: ...


class _YtDlpLogger:
    def __init__(self) -> None:
        self.last_error = ""

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.warning("yt-dlp: %s", _ANSI.sub("", msg))

    def error(self, msg: str) -> None:
        self.last_error = _ANSI.sub("", msg)
        log.debug("yt-dlp error: %s", self.last_error)


def clean_error(message: str) -> str:
    message = _ANSI.sub("", message).strip()
    return message.removeprefix("ERROR:").strip()


class YtDlpDownloader:
    def __init__(
        self,
        fmt: AudioFormat = "mp3",
        bitrate: int = 320,
        cookies_from_browser: str | None = None,
    ) -> None:
        self.fmt = fmt
        self.bitrate = bitrate
        self.cookies_from_browser = cookies_from_browser

    def options(
        self,
        dest_dir: Path,
        progress: ProgressCallback | None,
        cancel: threading.Event | None,
        logger: _YtDlpLogger,
    ) -> dict[str, Any]:
        def progress_hook(status: dict[str, Any]) -> None:
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled("cancelled")
            if progress is None:
                return
            if status.get("status") == "downloading":
                total = status.get("total_bytes") or status.get("total_bytes_estimate")
                done = status.get("downloaded_bytes") or 0
                if total:
                    progress(min(1.0, done / total), "downloading")
            elif status.get("status") == "finished":
                progress(1.0, "downloading")

        def postprocessor_hook(status: dict[str, Any]) -> None:
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled("cancelled")
            if progress is not None and status.get("status") == "started":
                progress(1.0, "converting")

        options: dict[str, Any] = {
            "format": format_selector(self.fmt),
            "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
            "paths": {"home": str(dest_dir), "temp": str(dest_dir)},
            "postprocessors": postprocessors(self.fmt, self.bitrate),
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [postprocessor_hook],
            "logger": logger,
            "quiet": True,
            "no_warnings": False,
            "noprogress": True,
            "noplaylist": True,
            "overwrites": True,
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": 30,
            "cachedir": False,
        }
        if self.cookies_from_browser:
            options["cookiesfrombrowser"] = (self.cookies_from_browser,)
        return options

    def download(
        self,
        video_id: str,
        dest_dir: Path,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> Path:
        import yt_dlp

        dest_dir.mkdir(parents=True, exist_ok=True)
        logger = _YtDlpLogger()
        url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            with yt_dlp.YoutubeDL(self.options(dest_dir, progress, cancel, logger)) as ydl:
                info = ydl.extract_info(url, download=True)
        except DownloadCancelled:
            raise
        except Exception as exc:
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled("cancelled") from exc
            raise DownloadError(clean_error(str(exc)) or logger.last_error) from exc
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled("cancelled")
        return self._find_output(info, dest_dir, video_id)

    def _find_output(self, info: Any, dest_dir: Path, video_id: str) -> Path:
        wanted = "." + extension(self.fmt)
        if isinstance(info, dict):
            for item in info.get("requested_downloads") or []:
                path = Path(item.get("filepath") or "")
                if path.suffix == wanted and path.exists():
                    return path
        matches = sorted(dest_dir.glob(f"{video_id}*{wanted}"))
        if matches:
            return matches[0]
        raise DownloadError(f"yt-dlp finished but no {wanted} file was produced for {video_id}")
