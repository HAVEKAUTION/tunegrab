"""yt-dlp wrapper with a fake YoutubeDL (no network)."""

import threading
from pathlib import Path
from typing import Any

import pytest

from tunegrab.download.convert import format_selector, postprocessors
from tunegrab.download.ytdlp import (
    DownloadCancelled,
    DownloadError,
    YtDlpDownloader,
    clean_error,
)


def test_postprocessors() -> None:
    assert postprocessors("mp3", 256) == [
        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "256"}
    ]
    assert postprocessors("m4a", 320) == [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}]
    assert format_selector("opus").startswith("bestaudio[acodec=opus]")
    assert format_selector("m4a").startswith("bestaudio[ext=m4a]")


class FakeYDL:
    """Mimics yt_dlp.YoutubeDL: calls hooks, writes the output file."""

    behaviour: dict[str, Any] = {}

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options
        FakeYDL.behaviour["options"] = options

    def __enter__(self) -> "FakeYDL":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def extract_info(self, url: str, download: bool) -> dict[str, Any]:
        FakeYDL.behaviour["url"] = url
        if error := FakeYDL.behaviour.get("error"):
            raise error
        home = Path(self.options["paths"]["home"])
        for hook in self.options["progress_hooks"]:
            hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
            hook({"status": "finished"})
        for hook in self.options["postprocessor_hooks"]:
            hook({"status": "started", "postprocessor": "ExtractAudio"})
        out = home / "vid12345678.mp3"
        out.write_bytes(b"audio")
        filepath = str(out) if FakeYDL.behaviour.get("report_path", True) else ""
        return {"requested_downloads": [{"filepath": filepath}]}


@pytest.fixture(autouse=True)
def fake_ydl(monkeypatch: pytest.MonkeyPatch) -> None:
    import yt_dlp

    FakeYDL.behaviour = {}
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)


def test_download_reports_progress(tmp_path: Path) -> None:
    events: list[tuple[float, str]] = []
    path = YtDlpDownloader("mp3", 320).download(
        "vid12345678", tmp_path / "d", progress=lambda f, p: events.append((f, p))
    )
    assert path == tmp_path / "d" / "vid12345678.mp3"
    assert events == [(0.5, "downloading"), (1.0, "downloading"), (1.0, "converting")]
    options = FakeYDL.behaviour["options"]
    assert options["noplaylist"] and options["format"] == "bestaudio/best"
    assert FakeYDL.behaviour["url"].endswith("watch?v=vid12345678")
    assert "cookiesfrombrowser" not in options


def test_cookies_option(tmp_path: Path) -> None:
    YtDlpDownloader("mp3", 320, cookies_from_browser="firefox").download("vid12345678", tmp_path)
    assert FakeYDL.behaviour["options"]["cookiesfrombrowser"] == ("firefox",)


def test_finds_file_when_not_reported(tmp_path: Path) -> None:
    FakeYDL.behaviour["report_path"] = False
    assert YtDlpDownloader().download("vid12345678", tmp_path).name == "vid12345678.mp3"


def test_missing_output(tmp_path: Path) -> None:
    with pytest.raises(DownloadError, match=r"no .m4a"):
        YtDlpDownloader("m4a").download("vid12345678", tmp_path)


def test_errors_are_wrapped(tmp_path: Path) -> None:
    FakeYDL.behaviour["error"] = RuntimeError("\x1b[0;31mERROR:\x1b[0m Video unavailable")
    with pytest.raises(DownloadError, match=r"^Video unavailable$"):
        YtDlpDownloader().download("vid12345678", tmp_path)


def test_cancel(tmp_path: Path) -> None:
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(DownloadCancelled):
        YtDlpDownloader().download("vid12345678", tmp_path, cancel=cancel)


def test_clean_error() -> None:
    assert clean_error("\x1b[31mERROR: boom\x1b[0m ") == "boom"
