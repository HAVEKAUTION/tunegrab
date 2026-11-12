"""yt-dlp wrapper with a fake YoutubeDL (no network)."""

import threading
from pathlib import Path
from typing import Any

import pytest

from tunegrab.download.convert import format_selector, postprocessors
from tunegrab.download.ytdlp import (
    CLIENT_LADDER,
    DownloadCancelled,
    DownloadError,
    SourceInfo,
    YtDlpDownloader,
    classify_error,
    clean_error,
    run_ladder,
    source_info,
)


def test_postprocessors() -> None:
    assert postprocessors("mp3", 256) == [
        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "256"}
    ]
    assert postprocessors("m4a", 320) == [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}]
    assert format_selector("opus").startswith("bestaudio[acodec=opus]")
    assert format_selector("m4a").startswith("bestaudio[ext=m4a]")


def test_selectors_avoid_drc_but_fall_back() -> None:
    for fmt in ("mp3", "m4a", "opus", "flac"):
        selector = format_selector(fmt)  # type: ignore[arg-type]
        assert selector.split("/")[0].endswith("[format_id!$=-drc]")
        assert selector.endswith("bestaudio/best")


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
        FakeYDL.behaviour.setdefault("clients", []).append(
            (self.options.get("extractor_args") or {}).get("youtube", {}).get("player_client")
        )
        for warning in FakeYDL.behaviour.get("warnings", []):
            self.options["logger"].warning(warning)
        if error := FakeYDL.behaviour.get("error"):
            raise error
        if errors := FakeYDL.behaviour.get("errors"):
            raise errors.pop(0)
        home = Path(self.options["paths"]["home"])
        assert not Path(self.options["outtmpl"]).is_absolute()
        for hook in self.options["progress_hooks"]:
            hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
            hook({"status": "finished"})
        for hook in self.options["postprocessor_hooks"]:
            hook({"status": "started", "postprocessor": "ExtractAudio"})
        out = home / "vid12345678.mp3"
        out.write_bytes(b"audio")
        filepath = str(out) if FakeYDL.behaviour.get("report_path", True) else ""
        info = {"requested_downloads": [{"filepath": filepath}]}
        formats = FakeYDL.behaviour.get("formats")
        info.update(formats.pop(0) if formats else {"format_id": "251", "acodec": "opus"})
        return info


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
    assert options["noplaylist"] and options["format"].startswith("bestaudio")
    assert FakeYDL.behaviour["url"].endswith("watch?v=vid12345678")
    assert "cookiesfrombrowser" not in options
    assert options["cachedir"] is False
    assert list(options["js_runtimes"]) == ["deno", "node", "bun", "quickjs"]
    assert "extractor_args" not in options  # first try: yt-dlp's own default clients


def test_cookies_option(tmp_path: Path) -> None:
    YtDlpDownloader("mp3", 320, cookies_from_browser="firefox").download("vid12345678", tmp_path)
    assert FakeYDL.behaviour["options"]["cookiesfrombrowser"] == ("firefox",)


def test_finds_file_when_not_reported(tmp_path: Path) -> None:
    FakeYDL.behaviour["report_path"] = False
    assert YtDlpDownloader("mp3").download("vid12345678", tmp_path).name == "vid12345678.mp3"


def test_missing_output(tmp_path: Path) -> None:
    with pytest.raises(DownloadError, match=r"no .m4a"):
        YtDlpDownloader("m4a").download("vid12345678", tmp_path)


def test_errors_are_wrapped(tmp_path: Path) -> None:
    FakeYDL.behaviour["error"] = RuntimeError("\x1b[0;31mERROR:\x1b[0m Video unavailable")
    with pytest.raises(DownloadError, match=r"^Video unavailable$") as caught:
        YtDlpDownloader("mp3").download("vid12345678", tmp_path)
    assert caught.value.kind == "unavailable"
    assert len(FakeYDL.behaviour["clients"]) == 1  # no point trying other clients


def test_blocked_tries_other_clients(tmp_path: Path) -> None:
    FakeYDL.behaviour["errors"] = [RuntimeError("ERROR: HTTP Error 403: Forbidden")]
    seen: list[SourceInfo] = []
    path = YtDlpDownloader("mp3").download("vid12345678", tmp_path, source=seen.append)
    assert path.exists()
    assert FakeYDL.behaviour["clients"] == [None, ["default", "-web_embedded"]]
    assert seen[0].client == "default,-web_embedded"


DEGRADED = "n challenge solving failed: Some formats may be missing"


def test_low_quality_tries_other_clients(tmp_path: Path) -> None:
    FakeYDL.behaviour["warnings"] = [DEGRADED]
    FakeYDL.behaviour["formats"] = [
        {"format_id": "18", "acodec": "mp4a.40.2", "abr": 96},
        {"format_id": "251", "acodec": "opus", "abr": 129.4},
    ]
    seen: list[SourceInfo] = []
    YtDlpDownloader("mp3").download("vid12345678", tmp_path, source=seen.append)
    assert len(FakeYDL.behaviour["clients"]) == 2
    assert seen[0].label == "Opus 129 kbps" and not seen[0].low_quality


def test_genuinely_low_quality_video_is_accepted_at_once(tmp_path: Path) -> None:
    FakeYDL.behaviour["formats"] = [{"format_id": "18", "acodec": "mp4a.40.2", "abr": 96}]
    seen: list[SourceInfo] = []
    YtDlpDownloader("mp3").download("vid12345678", tmp_path, source=seen.append)
    assert len(FakeYDL.behaviour["clients"]) == 1  # no "formats missing" warning
    assert seen[0].low_quality and not seen[0].worth_retrying


def test_low_quality_kept_when_later_client_errors(tmp_path: Path) -> None:
    FakeYDL.behaviour["warnings"] = [DEGRADED]
    FakeYDL.behaviour["formats"] = [{"format_id": "18", "acodec": "mp4a.40.2", "abr": 96}]
    seen: list[SourceInfo] = []

    class Failing(FakeYDL):
        def extract_info(self, url: str, download: bool) -> dict[str, Any]:
            if FakeYDL.behaviour.get("clients"):
                FakeYDL.behaviour["clients"].append("x")
                raise RuntimeError("ERROR: No video formats found!")
            return super().extract_info(url, download)

    import yt_dlp

    original = yt_dlp.YoutubeDL
    yt_dlp.YoutubeDL = Failing  # type: ignore[misc]
    try:
        path = YtDlpDownloader("mp3").download("vid12345678", tmp_path, source=seen.append)
    finally:
        yt_dlp.YoutubeDL = original  # type: ignore[misc]
    assert path.exists() and seen[0].format_id == "18"


def test_low_quality_kept_when_nothing_better(tmp_path: Path) -> None:
    FakeYDL.behaviour["warnings"] = [DEGRADED]
    FakeYDL.behaviour["formats"] = [{"format_id": "18", "acodec": "mp4a.40.2", "abr": 96}] * 3
    seen: list[SourceInfo] = []
    YtDlpDownloader("mp3").download("vid12345678", tmp_path, source=seen.append)
    assert len(FakeYDL.behaviour["clients"]) == len(CLIENT_LADDER)
    assert seen[0].low_quality and seen[0].label == "AAC 96 kbps (low quality source)"


def test_rate_limit_is_not_retried_with_other_clients(tmp_path: Path) -> None:
    FakeYDL.behaviour["error"] = RuntimeError("ERROR: HTTP Error 429: Too Many Requests")
    with pytest.raises(DownloadError) as caught:
        YtDlpDownloader("mp3").download("vid12345678", tmp_path)
    assert caught.value.kind == "rate_limited"
    assert len(FakeYDL.behaviour["clients"]) == 1


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("Video unavailable. This video is private", "unavailable"),
        ("The uploader has not made this video available in your country", "unavailable"),
        ("Sign in to confirm your age", "unavailable"),
        ("HTTP Error 429: Too Many Requests", "rate_limited"),
        ("Sign in to confirm you're not a bot", "rate_limited"),
        ("HTTP Error 403: Forbidden", "blocked"),
        ("Requested format is not available", "blocked"),
        ("n challenge solving failed: Some formats may be missing", "blocked"),
        ("The downloaded file is empty", "blocked"),
        ("[Errno 28] No space left on device", "other"),
    ],
)
def test_classify_error(message: str, kind: str) -> None:
    assert classify_error(message) == kind


def test_run_ladder_raises_last_blocked_error() -> None:
    def attempt(client: tuple[str, ...] | None) -> tuple[Path, SourceInfo]:
        raise DownloadError(f"403 with {client}", kind="blocked")

    with pytest.raises(DownloadError, match="web_safari"):
        run_ladder(attempt, None)


def test_source_info_prefers_audio_part_of_merged_formats() -> None:
    info = {
        "format_id": "137+140",
        "requested_formats": [
            {"format_id": "137", "acodec": "none", "tbr": 4000},
            {"format_id": "140", "acodec": "mp4a.40.2", "abr": 129.5},
        ],
    }
    assert source_info(info) == SourceInfo("140", "mp4a.40.2", 129.5)
    assert source_info(None) == SourceInfo(None, None, None)


def test_cancel(tmp_path: Path) -> None:
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(DownloadCancelled):
        YtDlpDownloader("mp3").download("vid12345678", tmp_path, cancel=cancel)


def test_clean_error() -> None:
    assert clean_error("\x1b[31mERROR: boom\x1b[0m ") == "boom"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (DownloadError("HTTP Error 403: Forbidden"), "update yt-dlp in Settings"),
        (DownloadError("n challenge solving failed"), "update yt-dlp in Settings"),
        (DownloadError("Video unavailable"), "Video unavailable"),
        (DownloadError("HTTP Error 429: Too Many Requests"), "Too Many Requests"),
        (DownloadError("network hiccup"), "network hiccup"),
        (OSError("boom"), "boom"),
    ],
)
def test_describe_failure_names_the_fix_only_for_breakage(
    error: BaseException, expected: str
) -> None:
    from tunegrab.download.ytdlp import describe_failure

    text = describe_failure(error)
    assert expected in text
    assert ("update yt-dlp" in text) == (expected == "update yt-dlp in Settings")


def test_describe_failure_looks_through_wrapping() -> None:
    from tunegrab.download.ytdlp import describe_failure

    try:
        try:
            raise DownloadError("HTTP Error 403: Forbidden")
        except DownloadError as inner:
            raise RuntimeError("HTTP Error 403: Forbidden") from inner
    except RuntimeError as outer:
        assert "update yt-dlp" in describe_failure(outer)


@pytest.mark.parametrize(
    "message",
    [
        "Sign in to confirm your age. This video may be inappropriate for some users.",
        "This video is age-restricted",
    ],
)
def test_age_restricted_failures_point_to_youtube_sign_in(message: str) -> None:
    from tunegrab.download.ytdlp import AGE_HINT, describe_failure

    assert describe_failure(DownloadError(message)) == AGE_HINT
    assert "YouTube sign-in in Settings" in AGE_HINT


@pytest.mark.parametrize(
    "message",
    [
        "Could not copy Chrome cookie database. See https://github.com/yt-dlp/yt-dlp/issues/7271",
        "Failed to decrypt with DPAPI",
        "could not find firefox cookies database in C:/x",
    ],
)
def test_unreadable_browser_cookies_say_what_to_do(message: str) -> None:
    from tunegrab.download.ytdlp import COOKIES_HINT, describe_failure

    text = describe_failure(DownloadError(message))
    assert text.startswith(COOKIES_HINT) and message in text


def test_mp3_is_v0_unless_a_bitrate_was_chosen() -> None:
    [auto] = postprocessors("mp3", None)
    assert auto["preferredquality"] == "0"  # yt-dlp: under 10 = VBR quality; 0 = LAME V0
    assert postprocessors("mp3", 192)[0]["preferredquality"] == "192"
    assert YtDlpDownloader().fmt == "m4a" and YtDlpDownloader().bitrate is None


def _picked(fmt: str, formats: list[dict[str, Any]]) -> list[str]:
    from yt_dlp.YoutubeDL import YoutubeDL  # the real one (other tests swap in a fake)

    ydl = YoutubeDL({"quiet": True})
    info: dict[str, Any] = {"formats": [dict(f) for f in formats], "extractor": "youtube"}
    ydl.sort_formats(info)  # type: ignore[attr-defined]
    select = ydl.build_format_selector(format_selector(fmt))  # type: ignore[arg-type]
    ctx = {"formats": info["formats"], "has_merged_format": False, "incomplete_formats": False}
    return [f["format_id"] for f in select(ctx)]


def test_premium_formats_are_picked_when_offered() -> None:
    def audio(fid: str, ext: str, acodec: str, abr: int) -> dict[str, Any]:
        return {"format_id": fid, "ext": ext, "acodec": acodec, "vcodec": "none", "abr": abr,
                "url": "https://x", "protocol": "https"}  # fmt: skip

    free = [
        audio("139", "m4a", "mp4a.40.5", 48),
        audio("140", "m4a", "mp4a.40.2", 129),
        audio("140-drc", "m4a", "mp4a.40.2", 129),
        audio("251", "webm", "opus", 135),
    ]
    premium = [*free, audio("141", "m4a", "mp4a.40.2", 256), audio("774", "webm", "opus", 256)]
    assert (_picked("m4a", free), _picked("m4a", premium)) == (["140"], ["141"])
    assert (_picked("opus", free), _picked("opus", premium)) == (["251"], ["774"])
    assert _picked("mp3", premium) == ["774"]
