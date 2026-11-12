"""Whole-job checks: missing folder, full disk."""

from __future__ import annotations

import asyncio
import errno
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import FakeDownloader, make_pipeline, make_track
from tunegrab import preflight
from tunegrab.download.ytdlp import DownloadError
from tunegrab.models import TaskStatus
from tunegrab.preflight import DISK_FULL, JobAborted, check_folder, is_disk_full

ALBUM = "spotify:album:4m2880jivSbbyEGAKfITCa"


def test_is_disk_full() -> None:
    assert is_disk_full(OSError(errno.ENOSPC, "No space left on device"))
    assert is_disk_full(DownloadError("ffmpeg: Error writing trailer: No space left on device"))
    assert is_disk_full(OSError(28, "There is not enough space on the disk"))
    wrapped = RuntimeError("tagging failed")
    wrapped.__cause__ = OSError(errno.ENOSPC, "full")
    assert is_disk_full(wrapped)
    assert not is_disk_full(DownloadError("HTTP Error 403: Forbidden"))


def test_check_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    check_folder(tmp_path / "not" / "yet" / "created")  # a missing subfolder is fine
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(JobAborted, match="isn't available"):
        check_folder(blocker / "music")

    class Usage:
        free = 10

    monkeypatch.setattr(preflight.shutil, "disk_usage", lambda path: Usage())
    with pytest.raises(JobAborted, match="disk is full"):
        check_folder(tmp_path)


def test_unusable_folder_stops_the_job_before_any_download(tmp_path: Path) -> None:
    blocker = tmp_path / "lib"
    blocker.write_text("not a folder")
    pipeline, _ = make_pipeline(tmp_path)
    job = pipeline.build_job(ALBUM)
    with pytest.raises(JobAborted):
        asyncio.run(pipeline.run(job))
    downloader = pipeline.downloader
    assert isinstance(downloader, FakeDownloader) and downloader.calls == 0


def test_disk_full_stops_the_job_and_leaves_the_rest_retryable(tmp_path: Path) -> None:
    tracks = [make_track(n) for n in (1, 2, 3, 4)]
    pipeline, archive = make_pipeline(tmp_path, tracks=tracks, threads=1)
    downloader = pipeline.downloader
    assert isinstance(downloader, FakeDownloader)
    real = downloader.download

    def fill_disk(video_id: str, *args: Any, **kwargs: Any) -> Path:
        if downloader.calls >= 1:
            downloader.calls += 1
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(video_id, *args, **kwargs)

    downloader.download = fill_disk  # type: ignore[method-assign]
    job = asyncio.run(pipeline.run(pipeline.build_job(ALBUM)))
    assert job.error == DISK_FULL and not job.cancelled
    assert [t.status for t in job.tasks] == [TaskStatus.DONE] + [TaskStatus.FAILED] * 3
    assert job.tasks[1].error == DISK_FULL
    assert all("disk is full" in (t.error or "") for t in job.tasks[1:])
    assert downloader.calls == 2  # stopped right after the disk filled up
    assert len(archive.open_failures()) == 3  # "Retry N failed" picks them up later
