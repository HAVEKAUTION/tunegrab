from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from tests.fakes import (
    PLAYLIST_ID,
    FakeAudio,
    FakeDownloader,
    FakeTagger,
    candidate_for,
    make_pipeline,
    make_track,
)
from tunegrab.library.playlists import read_m3u8
from tunegrab.models import Job, TaskStatus, Track
from tunegrab.pipeline import TMP_DIR_NAME, Pipeline

PLAYLIST = f"https://open.spotify.com/playlist/{PLAYLIST_ID}"
ALBUM = "spotify:album:4m2880jivSbbyEGAKfITCa"


def run(pipeline: Pipeline, text: str) -> Job:
    return asyncio.run(pipeline.run(pipeline.build_job(text)))


def statuses(job: Job) -> list[TaskStatus]:
    return [t.status for t in job.tasks]


def library_files(tmp: Path) -> list[Path]:
    return sorted(
        p for p in (tmp / "lib").rglob("*") if p.is_file() and TMP_DIR_NAME not in p.parts
    )


def test_album_downloads_tagged_files(tmp_path: Path) -> None:
    pipeline, archive = make_pipeline(tmp_path)
    events: list[str] = []
    pipeline.on_event = lambda job, task: events.append(task.status.value if task else "job")
    job = run(pipeline, ALBUM)
    assert statuses(job) == [TaskStatus.DONE] * 3
    files = library_files(tmp_path)
    assert [f.relative_to(tmp_path / "lib").as_posix() for f in files] == [
        f"Artist {n}/Album {n} (2020)/{n:02d} - Song {n}.mp3" for n in (1, 2, 3)
    ]
    assert all(f.read_bytes() == b"audio" for f in files)
    tagger = pipeline.tagger
    assert isinstance(tagger, FakeTagger) and len(tagger.tagged) == 3
    assert tagger.tagged[0][2] == b"\xff\xd8\xffcover"  # cover passed through
    assert archive.stats()["downloads"] == 3
    assert {"matching", "downloading", "converting", "tagging", "done"} <= set(events)
    assert events[0] == "job" and events[-1] == "job"
    assert job.finished_at is not None and not job.cancelled
    assert not (tmp_path / "lib" / TMP_DIR_NAME).exists()  # temp dirs cleaned


def test_rerun_skips_everything(tmp_path: Path) -> None:
    pipeline, _ = make_pipeline(tmp_path)
    run(pipeline, ALBUM)
    downloader = FakeDownloader()
    pipeline.downloader = downloader
    job = run(pipeline, ALBUM)
    assert statuses(job) == [TaskStatus.SKIPPED] * 3 and downloader.calls == 0
    assert job.tasks[0].message == "already downloaded"


def test_deleted_file_is_downloaded_again(tmp_path: Path) -> None:
    pipeline, _ = make_pipeline(tmp_path)
    run(pipeline, ALBUM)
    library_files(tmp_path)[0].unlink()
    assert statuses(run(pipeline, ALBUM)).count(TaskStatus.DONE) == 1


def test_other_format_is_not_skipped(tmp_path: Path) -> None:
    pipeline, _ = make_pipeline(tmp_path)
    run(pipeline, ALBUM)
    pipeline.settings = pipeline.settings.model_copy(update={"format": "m4a"})

    class M4ADownloader(FakeDownloader):
        def download(self, video_id, dest_dir, progress=None, cancel=None, source=None):  # type: ignore[no-untyped-def]
            out = super().download(video_id, dest_dir, progress, cancel)
            return out.rename(out.with_suffix(".m4a"))

    pipeline.downloader = M4ADownloader()
    assert statuses(run(pipeline, ALBUM)) == [TaskStatus.DONE] * 3
    assert len(library_files(tmp_path)) == 6


def test_retries_then_succeeds(tmp_path: Path) -> None:
    downloader = FakeDownloader(fail_times=2)
    pipeline, archive = make_pipeline(
        tmp_path, tracks=[make_track(1)], downloader=downloader, retries=2
    )
    job = run(pipeline, ALBUM)
    assert statuses(job) == [TaskStatus.DONE] and job.tasks[0].attempts == 3
    assert archive.open_failures() == []


def test_failure_recorded_and_job_continues(tmp_path: Path) -> None:
    downloader = FakeDownloader(fail_times=1)
    pipeline, archive = make_pipeline(
        tmp_path, tracks=[make_track(1), make_track(2)], downloader=downloader, retries=0, threads=1
    )
    job = run(pipeline, ALBUM)
    assert sorted(statuses(job)) == sorted([TaskStatus.FAILED, TaskStatus.DONE])
    failed = next(t for t in job.tasks if t.status == TaskStatus.FAILED)
    assert failed.failed_stage == "downloading" and "network hiccup" in (failed.error or "")
    [record] = archive.open_failures()
    assert record.key == failed.track.key and record.collection_name == "Fake Album"
    # a later success resolves the failure
    pipeline.downloader = FakeDownloader()
    run(pipeline, ALBUM)
    assert archive.open_failures() == []


def test_no_match_is_not_retried(tmp_path: Path) -> None:
    track = make_track(1)
    audio = FakeAudio(results={track.title: [candidate_for(track, title="Other", artists=["Z"])]})
    pipeline, _ = make_pipeline(tmp_path, tracks=[track], audio=audio, retries=3)
    job = run(pipeline, ALBUM)
    task = job.tasks[0]
    assert task.status == TaskStatus.FAILED and task.attempts == 1
    assert task.failed_stage == "matching" and "tunegrab fix" in (task.error or "")
    assert pipeline.downloader.calls == 0  # type: ignore[attr-defined]


def test_incomplete_tracks_are_enriched(tmp_path: Path) -> None:
    bare = Track(spotify_id="1" * 22, title="Song 1", artists=["Artist 1"], duration_ms=200_000)
    pipeline, _ = make_pipeline(tmp_path, tracks=[bare])
    job = run(pipeline, PLAYLIST)
    assert job.tasks[0].track.album == "Enriched" and job.tasks[0].status == TaskStatus.DONE


def test_playlist_m3u8_written(tmp_path: Path) -> None:
    pipeline, archive = make_pipeline(tmp_path)
    run(pipeline, PLAYLIST)
    m3u = tmp_path / "lib" / "Playlists" / "Fake Playlist.m3u8"
    entries = read_m3u8(m3u)
    assert [p.name for p in entries] == ["01 - Song 1.mp3", "02 - Song 2.mp3", "03 - Song 3.mp3"]
    assert all(p.exists() for p in entries)
    assert [t.title for _, t in archive.playlist_members(PLAYLIST_ID)] == [
        "Song 1",
        "Song 2",
        "Song 3",
    ]


def test_folder_mode(tmp_path: Path) -> None:
    pipeline, _ = make_pipeline(tmp_path, playlist_mode="folder")
    run(pipeline, ALBUM)  # albums still go to the library
    job = run(pipeline, PLAYLIST)  # already downloaded -> copied into the playlist folder
    assert statuses(job) == [TaskStatus.SKIPPED] * 3
    folder = tmp_path / "lib" / "Playlists" / "Fake Playlist"
    assert sorted(p.name for p in folder.iterdir()) == [
        f"{n:03d} - Artist {n} - Song {n}.mp3" for n in (1, 2, 3)
    ]
    assert not (tmp_path / "lib" / "Playlists" / "Fake Playlist.m3u8").exists()


def test_name_collision_gets_suffix(tmp_path: Path) -> None:
    twins = [make_track(1), make_track(1, spotify_id="9" * 22)]
    pipeline, _ = make_pipeline(tmp_path, tracks=twins, threads=1)
    run(pipeline, ALBUM)
    assert sorted(p.name for p in library_files(tmp_path)) == [
        "01 - Song 1 (2).mp3",
        "01 - Song 1.mp3",
    ]


def test_cancel_mid_download_leaves_no_files(tmp_path: Path) -> None:
    block, started = threading.Event(), threading.Event()
    downloader = FakeDownloader(block=block, started=started)
    pipeline, archive = make_pipeline(tmp_path, downloader=downloader, threads=1)
    job = pipeline.build_job(ALBUM)

    async def main() -> None:
        runner = asyncio.create_task(pipeline.run(job))
        await asyncio.to_thread(started.wait, 5)
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner

    asyncio.run(main())
    assert job.cancelled and all(t.status == TaskStatus.SKIPPED for t in job.tasks)
    assert {t.message for t in job.tasks} == {"cancelled"}
    assert library_files(tmp_path) == []  # the half-written file was discarded
    assert archive.open_failures() == [] and archive.stats()["downloads"] == 0


def test_search_query_job(tmp_path: Path) -> None:
    pipeline, _ = make_pipeline(tmp_path)
    with pytest.raises(ValueError):
        pipeline.build_job("Some Artist - Some Song")  # no query_lookup configured
    track = make_track(5, spotify_id=None)
    from tunegrab.models import Match

    pipeline.query_lookup = lambda q: (
        track,
        Match(track=track, candidate=candidate_for(track), score=100, reason="top"),
    )
    job = asyncio.run(pipeline.run(pipeline.build_job("Some Artist - Some Song")))
    assert job.collection is not None and job.collection.kind == "search"
    assert statuses(job) == [TaskStatus.DONE]
    assert pipeline.matcher.providers[0].calls == 0  # type: ignore[attr-defined]
    pipeline.query_lookup = lambda q: None
    with pytest.raises(LookupError):
        pipeline.build_job("nothing")


def test_unavailable_video_falls_back_to_next_match(tmp_path: Path) -> None:
    track = make_track(1)
    first = candidate_for(track, video_id="aaaaaaaaaaa")
    second = candidate_for(track, video_id="bbbbbbbbbbb", source="ytmusic_video")
    audio = FakeAudio(results={track.title: [first, second]})
    downloader = FakeDownloader(unavailable={"aaaaaaaaaaa"})
    pipeline, _ = make_pipeline(
        tmp_path, tracks=[track], audio=audio, downloader=downloader, retries=0
    )
    task = run(pipeline, ALBUM).tasks[0]
    assert task.status == TaskStatus.DONE
    assert downloader.downloaded == ["bbbbbbbbbbb"]
    assert task.match is not None and task.match.candidate is not None
    assert task.match.candidate.video_id == "bbbbbbbbbbb"
    assert task.source == "Opus 129 kbps" and not task.low_quality


def test_unavailable_with_no_alternative_fails_without_retrying(tmp_path: Path) -> None:
    track = make_track(1)
    only = candidate_for(track, video_id="aaaaaaaaaaa")
    audio = FakeAudio(results={track.title: [only]})
    downloader = FakeDownloader(unavailable={"aaaaaaaaaaa"})
    pipeline, _ = make_pipeline(
        tmp_path, tracks=[track], audio=audio, downloader=downloader, retries=3
    )
    task = run(pipeline, ALBUM).tasks[0]
    assert task.status == TaskStatus.FAILED and task.attempts == 1
    assert "Video unavailable" in (task.error or "")


def test_rate_limit_pauses_then_retries(tmp_path: Path) -> None:
    downloader = FakeDownloader(rate_limited_times=1)
    pipeline, _ = make_pipeline(tmp_path, tracks=[make_track(1)], downloader=downloader, retries=2)
    task = run(pipeline, ALBUM).tasks[0]
    assert task.status == TaskStatus.DONE and task.attempts == 2
    assert pipeline._cooldown_strikes == 0  # reset by the successful download


def test_rate_limit_cooldown_grows_and_is_capped(tmp_path: Path) -> None:
    pipeline, _ = make_pipeline(tmp_path, tracks=[make_track(1)])
    pipeline.rate_limit_pause = 60.0
    assert [pipeline._rate_limited() for _ in range(5)] == [60.0, 120.0, 240.0, 300.0, 300.0]


def test_empty_tmp_folder_is_removed(tmp_path: Path) -> None:
    pipeline, _ = make_pipeline(tmp_path, tracks=[make_track(1)])
    run(pipeline, ALBUM)
    assert not (tmp_path / "lib" / TMP_DIR_NAME).exists()
