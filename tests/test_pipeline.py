from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import (
    COVER_VIDEO,
    LIVE_VIDEO,
    PLAYLIST_ID,
    STUDIO_VIDEO,
    FakeAudio,
    FakeDownloader,
    FakeTagger,
    candidate_for,
    doubtful_candidates,
    make_pipeline,
    make_track,
)
from tunegrab.library.archive import Archive
from tunegrab.library.playlists import read_m3u8
from tunegrab.models import Collection, Job, Match, TaskStatus, Track, TrackTask
from tunegrab.pacing import Pacer
from tunegrab.pipeline import (
    RATE_LIMIT_GAVE_UP,
    RATE_LIMIT_GIVE_UP_AFTER,
    TMP_DIR_NAME,
    Pipeline,
)

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
    assert task.failed_stage == "matching" and "no match above 70" in (task.error or "")
    assert "tunegrab" not in (task.error or "")  # the app shows its own "Use a YouTube link"
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
    assert job.name == f"{track.title} – {', '.join(track.artists)}"  # what it picked
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


def test_rate_limit_pauses_then_retries_without_using_a_retry(tmp_path: Path) -> None:
    downloader = FakeDownloader(rate_limited_times=2)
    pipeline, _ = make_pipeline(tmp_path, tracks=[make_track(1)], downloader=downloader, retries=0)
    task = run(pipeline, ALBUM).tasks[0]
    assert task.status == TaskStatus.DONE and task.attempts == 1
    assert pipeline._cooldown_strikes == 0  # reset by the successful download


def test_rate_limit_cooldown_grows_then_blocks_then_gives_up(tmp_path: Path) -> None:
    pipeline, _ = make_pipeline(tmp_path, tracks=[make_track(1)])
    pipeline.rate_limit_pause = 60.0
    pipeline.rate_limit_block = 3600.0
    job = Job(input="x")
    assert [pipeline._rate_limited(job) for _ in range(4)] == [60.0, 120.0, 3600.0, 3600.0]
    assert job.paused_until is not None and pipeline.abort_reason is None
    assert pipeline._rate_limited(job) == 0.0
    assert pipeline.abort_reason == RATE_LIMIT_GAVE_UP and pipeline.cancel_event.is_set()


def test_hour_pause_can_be_ended_early(tmp_path: Path) -> None:
    pipeline, _ = make_pipeline(tmp_path, tracks=[make_track(1)])
    pipeline.rate_limit_block = 3600.0
    job = Job(input="x")
    assert pipeline.resume() is False  # nothing to resume
    for _ in range(3):
        pipeline._rate_limited(job)
    assert job.paused_until is not None
    assert pipeline.resume() is True
    task = TrackTask(track=make_track(1), position=1)
    pipeline._wait_for_cooldown(job, task)  # returns at once and clears the banner
    assert job.paused_until is None


def test_repeated_rate_limits_stop_the_job_and_leave_songs_retryable(tmp_path: Path) -> None:
    downloader = FakeDownloader(rate_limited_times=99)
    pipeline, archive = make_pipeline(
        tmp_path, tracks=[make_track(1), make_track(2)], downloader=downloader, retries=3
    )
    job = run(pipeline, ALBUM)
    # stopped knocking; the other song's thread may have been mid-attempt when it gave up
    assert RATE_LIMIT_GIVE_UP_AFTER <= downloader.calls <= RATE_LIMIT_GIVE_UP_AFTER + 1
    assert statuses(job) == [TaskStatus.FAILED, TaskStatus.FAILED]
    assert job.error == RATE_LIMIT_GAVE_UP
    assert len(archive.open_failures()) == 2


def test_downloads_are_paced(tmp_path: Path) -> None:
    clock = [0.0]
    pacer = Pacer(3600, burst=1, clock=lambda: clock[0])  # one start per second
    pipeline, _ = make_pipeline(tmp_path, tracks=[make_track(1), make_track(2)])
    pipeline.pacer = pacer
    waits: list[float] = []
    pipeline.cancel_event = WaitRecorder(waits, clock)
    job = run(pipeline, ALBUM)
    assert statuses(job) == [TaskStatus.DONE, TaskStatus.DONE]
    assert waits == [pytest.approx(1.0)]


class WaitRecorder(threading.Event):
    """A cancel event whose wait() advances a fake clock instead of sleeping."""

    def __init__(self, waits: list[float], clock: list[float]) -> None:
        super().__init__()
        self.waits, self.clock = waits, clock

    def wait(self, timeout: float | None = None) -> bool:
        if timeout:
            self.waits.append(timeout)
            self.clock[0] += timeout
        return self.is_set()


def test_empty_tmp_folder_is_removed(tmp_path: Path) -> None:
    pipeline, _ = make_pipeline(tmp_path, tracks=[make_track(1)])
    run(pipeline, ALBUM)
    assert not (tmp_path / "lib" / TMP_DIR_NAME).exists()


def test_duplicate_song_in_a_job_is_downloaded_once(tmp_path: Path) -> None:
    song = make_track(1)
    pipeline, _ = make_pipeline(tmp_path, tracks=[song, make_track(2), song], threads=4)
    job = run(pipeline, ALBUM)
    downloader = pipeline.downloader
    assert isinstance(downloader, FakeDownloader) and downloader.calls == 2
    assert statuses(job) == [TaskStatus.DONE, TaskStatus.DONE, TaskStatus.SKIPPED]
    assert job.tasks[2].message == "already downloaded"
    assert job.tasks[2].path == job.tasks[0].path
    assert len(library_files(tmp_path)) == 2


def _tag_library(pipeline: Pipeline, tmp_path: Path) -> None:
    """Make the fake tagger 'read' the tags of the files now in the library."""
    tagger = pipeline.tagger
    assert isinstance(tagger, FakeTagger)
    for path in library_files(tmp_path):
        number = int(path.name[:2])
        tagger.tags[path.name] = (f"Song {number}", f"Artist {number}")


def test_songs_already_in_the_library_are_adopted_after_history_loss(tmp_path: Path) -> None:
    pipeline, archive = make_pipeline(tmp_path)
    run(pipeline, ALBUM)
    _tag_library(pipeline, tmp_path)
    for record in archive.all_downloads():
        archive.forget_download(record.key)  # the history is gone, the files are not
    job = run(pipeline, ALBUM)
    assert statuses(job) == [TaskStatus.SKIPPED] * 3
    assert {t.message for t in job.tasks} == {"already in your library"}
    downloader = pipeline.downloader
    assert isinstance(downloader, FakeDownloader) and downloader.calls == 3  # not 6
    assert len(library_files(tmp_path)) == 3 and archive.stats()["downloads"] == 3


def test_failed_history_write_keeps_the_song(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline, archive = make_pipeline(tmp_path, tracks=[make_track(1)])
    real = archive.record_download
    calls = {"n": 0}

    def flaky(*args: object, **kwargs: object) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(archive, "record_download", flaky)
    job = run(pipeline, ALBUM)
    assert statuses(job) == [TaskStatus.DONE]
    assert job.tasks[0].message == "saved, but not added to the download history"
    _tag_library(pipeline, tmp_path)
    again = run(pipeline, ALBUM)
    assert statuses(again) == [TaskStatus.SKIPPED]
    assert [p.name for p in library_files(tmp_path)] == ["01 - Song 1.mp3"]  # no "(2)" copy


class RefusedDownloader(FakeDownloader):
    """Every player client was refused (what a YouTube player change looks like)."""

    def download(self, video_id: str, dest_dir: Path, *args: object, **kwargs: object) -> Path:
        from tunegrab.download.ytdlp import DownloadError

        raise DownloadError("ERROR: [youtube] abc: n challenge solving failed")


def test_refused_download_says_to_update_ytdlp(tmp_path: Path) -> None:
    from tunegrab.download.ytdlp import BREAKAGE_HINT

    pipeline, _ = make_pipeline(
        tmp_path, tracks=[make_track(1)], downloader=RefusedDownloader(), retries=0
    )
    task = run(pipeline, ALBUM).tasks[0]
    assert task.status == TaskStatus.FAILED
    assert (task.error or "").startswith("YouTube refused this download (")
    assert BREAKAGE_HINT in (task.error or "")


# --- match review ---------------------------------------------------------------------


def doubtful_pipeline(tmp_path: Path, **settings: Any) -> tuple[Pipeline, Archive, Track]:
    """One song whose best match is a live version (score 70), with two alternatives."""
    track = make_track(1)
    audio = FakeAudio(results={track.title: doubtful_candidates(track)})
    pipeline, archive = make_pipeline(tmp_path, tracks=[track], audio=audio, **settings)
    return pipeline, archive, track


def replacement_job(archive: Archive, track: Track, video_id: str) -> Job:
    record, saved = archive.get_download(track.key), archive.match_record(track.key)
    assert record is not None and saved is not None
    picked = next(a for a in saved.alternatives if a.candidate.video_id == video_id)
    match = Match(
        track=track,
        candidate=picked.candidate.model_copy(update={"source": "manual"}),
        score=100,
        reason="you chose this video",
    )
    task = TrackTask(track=track, position=1, match=match, replaces=str(record.path))
    collection = Collection(kind="track", name=track.title, tracks=[track])
    return Job(input=track.display, collection=collection, tasks=[task])


def test_sure_matches_are_not_flagged(tmp_path: Path) -> None:
    pipeline, archive = make_pipeline(tmp_path)
    job = run(pipeline, ALBUM)
    assert [t.review for t in job.tasks] == [None] * 3
    assert all(r.review is None and r.source == "Opus 129 kbps" for r in archive.all_downloads())


def test_doubtful_match_is_flagged_with_alternatives(tmp_path: Path) -> None:
    pipeline, archive, track = doubtful_pipeline(tmp_path)
    [task] = run(pipeline, ALBUM).tasks
    assert task.status == TaskStatus.DONE and task.review is not None and "live" in task.review
    assert task.match is not None and task.match.score == 70
    assert [a.candidate.video_id for a in task.match.alternatives] == [STUDIO_VIDEO, COVER_VIDEO]
    record = archive.get_download(track.key)
    assert record is not None and record.review == task.review and record.video_id == LIVE_VIDEO

    record.path.unlink()  # downloaded again from the remembered match: still offered
    [again] = run(pipeline, ALBUM).tasks
    assert again.match is not None and again.match.cached and again.review == task.review
    assert [a.candidate.video_id for a in again.match.alternatives] == [STUDIO_VIDEO, COVER_VIDEO]

    archive.confirm_match(track.key)  # the user kept it: never asked again
    record.path.unlink()
    [kept] = run(pipeline, ALBUM).tasks
    assert kept.status == TaskStatus.DONE and kept.review is None
    final = archive.get_download(track.key)
    assert final is not None and final.review is None


def test_chosen_match_replaces_the_file_in_place(tmp_path: Path) -> None:
    pipeline, archive, track = doubtful_pipeline(tmp_path)
    run(pipeline, ALBUM)
    old = archive.get_download(track.key)
    assert old is not None and old.path.read_bytes() == b"audio"
    assert isinstance(pipeline.downloader, FakeDownloader)
    pipeline.downloader.content[STUDIO_VIDEO] = b"studio"
    job = asyncio.run(pipeline.run(replacement_job(archive, track, STUDIO_VIDEO)))
    assert statuses(job) == [TaskStatus.DONE] and job.tasks[0].review is None
    assert library_files(tmp_path) == [old.path]  # same name, no "(2)" copy
    assert old.path.read_bytes() == b"studio"
    record = archive.get_download(track.key)
    assert record is not None and record.path == old.path
    assert record.video_id == STUDIO_VIDEO and record.review is None
    saved = archive.match_record(track.key)
    assert saved is not None and saved.manual and saved.candidate.video_id == STUDIO_VIDEO


def test_failed_replacement_keeps_the_old_file(tmp_path: Path) -> None:
    pipeline, archive, track = doubtful_pipeline(tmp_path, retries=0)
    run(pipeline, ALBUM)
    old = archive.get_download(track.key)
    assert old is not None
    pipeline.downloader = FakeDownloader(fail_times=9)
    job = asyncio.run(pipeline.run(replacement_job(archive, track, STUDIO_VIDEO)))
    assert statuses(job) == [TaskStatus.FAILED]
    assert library_files(tmp_path) == [old.path] and old.path.read_bytes() == b"audio"
    record = archive.get_download(track.key)
    assert record is not None and record.video_id == LIVE_VIDEO and record.review == old.review
    saved = archive.match_record(track.key)
    assert saved is not None and not saved.manual and saved.candidate.video_id == LIVE_VIDEO
    assert archive.open_failures() == []  # the song isn't missing: nothing to "retry failed"


def test_replacing_a_file_in_use_fails_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline, archive, track = doubtful_pipeline(tmp_path)
    run(pipeline, ALBUM)
    old = archive.get_download(track.key)
    assert old is not None
    real_replace = Path.replace

    def replace(self: Path, target: Path) -> Path:
        if Path(target) == old.path:
            raise PermissionError(13, "The file is in use", str(target))
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", replace)
    job = asyncio.run(pipeline.run(replacement_job(archive, track, STUDIO_VIDEO)))
    [task] = job.tasks
    assert task.status == TaskStatus.FAILED and task.attempts == 1  # not worth retrying
    assert task.error is not None and "another program is using it" in task.error
    assert library_files(tmp_path) == [old.path] and old.path.read_bytes() == b"audio"
