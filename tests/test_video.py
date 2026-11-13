"""Video downloads (tunegrab.video) with a fake yt-dlp: no network, no ffmpeg."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tunegrab import video
from tunegrab.config import Settings
from tunegrab.download.ytdlp import DownloadError
from tunegrab.models import TaskStatus
from tunegrab.pacing import Pacer
from tunegrab.urls import is_video_link
from tunegrab.video import (
    VideoDownloader,
    VideoEntry,
    VideoError,
    VideoOptions,
    VideoPipeline,
    VideoProbe,
    build_job,
    check_clip,
    clip_command,
    clip_stamp,
    final_path,
    parse_time,
    probe_info,
    video_format,
    video_label,
    video_sort,
)

VIDEO_INFO: dict[str, Any] = {
    "id": "dQw4w9WgXcQ",
    "title": "Never Gonna Give You Up",
    "uploader": "Rick Astley",
    "duration": 213,
    "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "extractor_key": "Youtube",
    "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg",
    "formats": [
        {"format_id": "137", "height": 1080, "vcodec": "avc1.640028"},
        {"format_id": "401", "height": 2160, "vcodec": "av01.0.12M.08"},
        {"format_id": "616-sr", "height": 4320, "vcodec": "vp09"},
        {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2"},
    ],
}


# --- small helpers -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("83", 83.0), ("1:23", 83.0), ("1:02:03", 3723.0), ("83.5", 83.5), ("", None), (None, None)],
)
def test_parse_time(text: str | None, seconds: float | None) -> None:
    assert parse_time(text) == seconds


@pytest.mark.parametrize("text", ["abc", "1:75", "-3", "1::2"])
def test_parse_time_rejects(text: str) -> None:
    with pytest.raises(VideoError):
        parse_time(text)


def test_clip_checks() -> None:
    assert check_clip(0, 30, 213) == (None, 30)  # from the start
    assert check_clip(10, 500, 213) == (10, None)  # past the end = to the end
    with pytest.raises(VideoError, match="end after"):
        check_clip(30, 10, 213)
    with pytest.raises(VideoError, match="only 3:33 long"):
        check_clip(300, None, 213)


def test_clip_names_are_windows_safe() -> None:
    assert clip_stamp(43) == "0m43s"
    assert clip_stamp(3723.5) == "1h02m03.5s"
    assert VideoOptions(start=43, end=52.5).clip_suffix() == " (clip 0m43s-0m52.5s)"
    assert VideoOptions(start=60).clip_suffix() == " (clip 1m00s-end)"
    assert VideoOptions().clip_suffix() == ""


def test_format_skips_upscaled_and_drc_but_falls_back() -> None:
    selector = video_format("1080")
    first = selector.split("/")[0]
    assert first == "bv*[height<=1080][format_id!$=-sr]+ba[format_id!$=-drc]"
    assert selector.endswith("bv*+ba/b")
    assert "height" not in video_format("best").split("/")[0]
    assert video_sort("720") == ["res:720", "vcodec:h264", "acodec:aac"]
    assert video_sort("best")[0] == "res"


def test_video_label() -> None:
    merged = {
        "requested_formats": [
            {"vcodec": "avc1.640028", "height": 1080, "fps": 60},
            {"vcodec": "none", "acodec": "mp4a.40.2"},
        ]
    }
    assert video_label(merged) == "1080p60 H.264"
    assert video_label({"vcodec": "vp09.00.50.08", "height": 2160, "fps": 30}) == "2160p VP9"
    assert video_label({"vcodec": "none"}) is None


def test_is_video_link() -> None:
    assert is_video_link("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert is_video_link("youtu.be/dQw4w9WgXcQ")
    assert is_video_link("https://vimeo.com/76979871")
    assert is_video_link("www.example.com/clip")
    assert not is_video_link("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC")
    assert not is_video_link("will.i.am")  # an artist, not a link
    assert not is_video_link("Daft Punk - One More Time")


# --- probing -----------------------------------------------------------------------------


def test_probe_single_video() -> None:
    found = probe_info("u", VIDEO_INFO)
    assert found.kind == "video" and found.uploader == "Rick Astley"
    assert found.heights == [2160, 1080]  # the upscaled 4320p copy isn't offered
    assert found.entries[0].duration_s == 213
    assert found.payload()["duration_s"] == 213


def test_probe_playlist_and_channel() -> None:
    playlist = {
        "_type": "playlist",
        "title": "Mix",
        "uploader": "Someone",
        "entries": [
            {
                "id": "aaaaaaaaaaa",
                "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
                "title": "A",
            },
            {"id": "bbbbbbbbbbb", "url": "https://www.youtube.com/watch?v=bbbbbbbbbbb"},
        ],
    }
    found = probe_info("u", playlist)
    assert found.kind == "playlist" and len(found.entries) == 2
    assert found.entries[1].title == "bbbbbbbbbbb" and found.entries[1].uploader == "Someone"
    channel = {
        "_type": "playlist",
        "entries": [{"_type": "url", "ie_key": "YoutubeTab", "id": "x", "url": "https://y/videos"}],
    }
    with pytest.raises(VideoError, match="channel page"):
        probe_info("u", channel)


def test_probe_rejects_non_links() -> None:
    with pytest.raises(VideoError, match="full link"):
        video.probe("not a link")


# --- downloading -------------------------------------------------------------------------


class FakeYDL:
    """Writes ``<id>.<ext>`` like yt-dlp and reports the chosen formats."""

    calls: list[dict[str, Any]] = []
    fail: list[Exception] = []

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options
        FakeYDL.calls.append(options)

    def __enter__(self) -> FakeYDL:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def extract_info(self, url: str, download: bool) -> dict[str, Any]:
        if FakeYDL.fail:
            raise FakeYDL.fail.pop(0)
        home = Path(self.options["paths"]["home"])
        ext = "mp4" if self.options.get("merge_output_format") else "mp3"
        out = home / f"dQw4w9WgXcQ.{ext}"
        out.write_bytes(b"media")
        return {
            "requested_downloads": [{"filepath": str(out)}],
            "requested_formats": [
                {"format_id": "137", "vcodec": "avc1.640028", "height": 1080, "fps": 25},
                {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 129.5},
            ],
        }


@pytest.fixture(autouse=True)
def fake_ydl(monkeypatch: pytest.MonkeyPatch) -> None:
    import yt_dlp

    FakeYDL.calls = []
    FakeYDL.fail = []
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)


def test_downloader_video_options(tmp_path: Path) -> None:
    downloader = VideoDownloader(VideoOptions(quality="720"))
    path, label, low = downloader.download("https://youtu.be/dQw4w9WgXcQ", tmp_path)
    assert path == tmp_path / "dQw4w9WgXcQ.mp4" and not low
    assert label == "1080p H.264 + AAC 130 kbps"
    options = FakeYDL.calls[0]
    assert options["merge_output_format"] == "mp4" and options["format_sort"][0] == "res:720"
    assert options["cachedir"] is False and not Path(options["outtmpl"]).is_absolute()


def test_downloader_audio_mode(tmp_path: Path) -> None:
    downloader = VideoDownloader(VideoOptions(mode="audio"), audio_format="mp3", bitrate=256)
    path, label, _ = downloader.download("https://youtu.be/dQw4w9WgXcQ", tmp_path)
    assert path.suffix == ".mp3" and label == "AAC 130 kbps"
    keys = [pp["key"] for pp in FakeYDL.calls[0]["postprocessors"]]
    assert keys == ["FFmpegExtractAudio", "FFmpegMetadata"]


def test_clip_command() -> None:
    source, target = Path("in.mp4"), Path("out.mp4")
    cmd = clip_command(source, target, VideoOptions(start=10, end=25))
    assert cmd[cmd.index("-ss") + 1] == "10.000" and cmd[cmd.index("-t") + 1] == "15.000"
    assert "libx264" in cmd and cmd[-1] == "out.mp4"
    audio = clip_command(source, target, VideoOptions(mode="audio", end=5))
    assert "-ss" not in audio and audio[audio.index("-c") + 1] == "copy"


def _probe(n: int = 1) -> VideoProbe:
    entries = [
        VideoEntry(f"vid{i:08d}", f"https://youtu.be/vid{i:08d}", f"Video {i}", "Chan", 60)
        for i in range(n)
    ]
    return VideoProbe(
        "https://youtu.be/x", "video" if n == 1 else "playlist", "T", "Chan", "Youtube", entries
    )


class StubDownloader(VideoDownloader):
    def __init__(self, fail: list[Exception] | None = None) -> None:
        super().__init__(VideoOptions())
        self.fail = fail or []
        self.urls: list[str] = []

    def download(
        self, url: str, dest_dir: Path, progress: Any = None, cancel: Any = None
    ) -> tuple[Path, str | None, bool]:  # type: ignore[override]
        self.urls.append(url)
        if self.fail:
            raise self.fail.pop(0)
        if progress:
            progress(0.5, "downloading")
        out = dest_dir / "x.mp4"
        dest_dir.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"video")
        return out, "1080p H.264 + AAC 130 kbps", False


def _pipeline(
    tmp_path: Path, stub: StubDownloader, n: int = 1, **settings: Any
) -> tuple[VideoPipeline, Any]:
    config = Settings(_env_file=None, output_dir=tmp_path / "lib", **settings)
    job, entries = build_job(_probe(n), VideoOptions())
    pipeline = VideoPipeline(
        config, VideoOptions(), entries, downloader=stub, retry_backoff=0, pacer=Pacer(0)
    )
    return pipeline, job


def test_pipeline_saves_and_skips_existing(tmp_path: Path) -> None:
    stub = StubDownloader()
    pipeline, job = _pipeline(tmp_path, stub, n=2)
    asyncio.run(pipeline.run(job))
    assert [t.status for t in job.tasks] == [TaskStatus.DONE, TaskStatus.DONE]
    first = Path(job.tasks[0].path or "")
    assert first == tmp_path / "lib" / "Videos" / "Chan" / "Video 0 [vid00000000].mp4"
    assert first.read_bytes() == b"video" and job.tasks[0].source.startswith("1080p")  # type: ignore[union-attr]
    assert not (tmp_path / "lib" / "Videos" / ".tunegrab-tmp").exists()
    pipeline, again = _pipeline(tmp_path, stub, n=2)
    asyncio.run(pipeline.run(again))
    assert [t.status for t in again.tasks] == [TaskStatus.SKIPPED] * 2


def test_pipeline_retries_then_fails_without_retrying_unavailable(tmp_path: Path) -> None:
    stub = StubDownloader(fail=[DownloadError("HTTP Error 403: Forbidden")])
    pipeline, job = _pipeline(tmp_path, stub, retries=1)
    asyncio.run(pipeline.run(job))
    assert job.tasks[0].status == TaskStatus.DONE and job.tasks[0].attempts == 2
    stub = StubDownloader(fail=[DownloadError("Video unavailable")] * 3)
    pipeline, job = _pipeline(tmp_path / "b", stub, retries=2)
    asyncio.run(pipeline.run(job))
    assert job.tasks[0].status == TaskStatus.FAILED and job.tasks[0].attempts == 1


def test_audio_mode_goes_to_the_music_folder(tmp_path: Path) -> None:
    config = Settings(_env_file=None, output_dir=tmp_path / "lib")
    entry = _probe().entries[0]
    options = VideoOptions(mode="audio")
    pipeline = VideoPipeline(config, options, {}, downloader=StubDownloader())
    assert pipeline.root == tmp_path / "lib"
    assert final_path(pipeline.root, entry, options, "mp3").name == "Video 0 [vid00000000].mp3"


def test_unexpected_error_fails_one_video_and_the_job_continues(tmp_path: Path) -> None:
    stub = StubDownloader(fail=[KeyError("formats")])
    pipeline, job = _pipeline(tmp_path, stub, n=2, retries=0)
    asyncio.run(pipeline.run(job))
    assert [t.status for t in job.tasks] == [TaskStatus.FAILED, TaskStatus.DONE]
    assert "KeyError" in (job.tasks[0].error or "")
    assert job.finished_at is not None


def test_refused_video_says_to_update_ytdlp(tmp_path: Path) -> None:
    from tunegrab.download.ytdlp import BREAKAGE_HINT

    stub = StubDownloader(fail=[DownloadError("HTTP Error 403: Forbidden")])
    pipeline, job = _pipeline(tmp_path, stub, retries=0)
    asyncio.run(pipeline.run(job))
    assert job.tasks[0].status == TaskStatus.FAILED
    assert BREAKAGE_HINT in (job.tasks[0].error or "")


# --- download history for videos (C4) -------------------------------------------------------
def _archived(tmp_path: Path, stub: StubDownloader, options: VideoOptions) -> tuple[Any, Any]:
    from tunegrab.library.archive import Archive

    archive = Archive(tmp_path / "archive.db")
    config = Settings(_env_file=None, output_dir=tmp_path / "lib", retries=0)
    job, entries = build_job(_probe(), options)
    pipeline = VideoPipeline(
        config, options, entries, downloader=stub, retry_backoff=0, pacer=Pacer(0), archive=archive
    )
    return pipeline, job


def test_saved_video_is_recorded_and_skipped_next_time(tmp_path: Path) -> None:
    stub = StubDownloader()
    pipeline, job = _archived(tmp_path, stub, VideoOptions())
    asyncio.run(pipeline.run(job))
    assert job.tasks[0].status == TaskStatus.DONE
    assert pipeline.archive is not None
    [record] = pipeline.archive.all_videos()
    assert record.url == "https://youtu.be/vid00000000" and record.exists
    assert record.options["mode"] == "video" and record.source.startswith("1080p")
    # renamed by the user: the history still knows where it is, so no second download
    moved = record.path.with_name("renamed.mp4")  # type: ignore[union-attr]
    record.path.rename(moved)  # type: ignore[union-attr]
    pipeline.archive.record_video(record.key, record.entry, record.options, None, moved, "x")
    again, job2 = _archived(tmp_path, stub, VideoOptions())
    asyncio.run(again.run(job2))
    assert job2.tasks[0].status == TaskStatus.SKIPPED and job2.tasks[0].path == str(moved)
    assert len(stub.urls) == 1
    # the same link with other options is a different download
    clip, job3 = _archived(tmp_path, stub, VideoOptions(quality="720"))
    asyncio.run(clip.run(job3))
    assert len(stub.urls) == 2


def test_failed_video_is_remembered_until_it_succeeds(tmp_path: Path) -> None:
    stub = StubDownloader(fail=[DownloadError("Video unavailable")])
    pipeline, job = _archived(tmp_path, stub, VideoOptions(mode="audio"))
    asyncio.run(pipeline.run(job))
    assert pipeline.archive is not None
    [failed] = pipeline.archive.video_failures()
    assert failed.error and "unavailable" in failed.error and failed.format == "mp3"
    assert failed.entry["title"] == "Video 0"
    ok, job2 = _archived(tmp_path, stub, VideoOptions(mode="audio"))
    asyncio.run(ok.run(job2))
    assert pipeline.archive.video_failures() == []
    assert len(pipeline.archive.all_videos()) == 1
