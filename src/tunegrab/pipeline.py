"""Orchestrates a Job: resolve -> match -> download -> convert -> tag -> archive.

Blocking work (HTTP, yt-dlp, ffmpeg, mutagen) runs in a bounded thread pool driven
by asyncio. Audio is downloaded into a private temp dir under the output folder and
only moved into the library after tagging succeeds, so an interrupted run never
leaves half-written files in the library.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from tunegrab.config import Settings
from tunegrab.download.convert import extension
from tunegrab.download.ytdlp import (
    DownloadCancelled,
    Downloader,
    DownloadError,
    SourceInfo,
)
from tunegrab.library.archive import Archive
from tunegrab.library.paths import move_into_place, render_path, template_fields
from tunegrab.library.playlists import playlist_file, write_m3u8
from tunegrab.matching.matcher import Matcher
from tunegrab.metadata.resolver import Resolver
from tunegrab.models import Candidate, Collection, Job, Match, TaskStatus, Track, TrackTask
from tunegrab.pacing import Pacer
from tunegrab.preflight import check_folder
from tunegrab.runner import Cancelled, EventCallback, TaskFailed, TaskRunner
from tunegrab.tagging.lyrics import Lyrics, write_lrc
from tunegrab.urls import is_spotify_input

log = logging.getLogger(__name__)

TMP_DIR_NAME = ".tunegrab-tmp"
STALE_TMP_AGE_S = 6 * 3600
RETRY_BACKOFF_S = 2.0
RATE_LIMIT_PAUSE_S = 60.0  # first pause when YouTube says "too many requests"
RATE_LIMIT_MAX_PAUSE_S = 300.0
# After this many "slow down" answers in one job YouTube is blocking the connection, which
# lasts about an hour; knocking again during it only makes it worse.
RATE_LIMIT_BLOCK_AFTER = 3
RATE_LIMIT_BLOCK_S = 3600.0
RATE_LIMIT_GIVE_UP_AFTER = 5
RATE_LIMIT_GAVE_UP = (
    "YouTube keeps refusing downloads from your connection. Wait an hour or two, then press Retry."
)
MAX_ALTERNATIVES = 3  # other videos to try when the chosen one is unavailable


class Tagger(Protocol):
    def tag(
        self, path: Path, track: Track, cover: bytes | None = None, lyrics: str | None = None
    ) -> None: ...

    def identify(self, path: Path) -> tuple[str, str] | None:
        """(title, artist) from an existing file's tags, if any."""
        ...


class LyricsSource(Protocol):
    def fetch(self, track: Track) -> Lyrics | None: ...


class QueryLookup(Protocol):
    """Turns a free-text search into (track metadata, chosen match)."""

    def __call__(self, query: str) -> tuple[Track, Match] | None: ...


@dataclass
class Pipeline(TaskRunner):
    settings: Settings
    resolver: Resolver
    matcher: Matcher
    downloader: Downloader
    tagger: Tagger
    archive: Archive
    lyrics: LyricsSource | None = None
    cover_fetcher: Callable[[str], bytes] | None = None
    query_lookup: QueryLookup | None = None
    on_event: EventCallback | None = None
    retry_backoff: float = RETRY_BACKOFF_S
    rate_limit_pause: float = RATE_LIMIT_PAUSE_S
    rate_limit_block: float = RATE_LIMIT_BLOCK_S
    # None: the process-wide pacer for YouTube (see tunegrab.pacing)
    pacer: Pacer | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    # Shared by all worker threads: no new download starts before this time.
    _cooldown_until: float = field(default=0.0, init=False, repr=False)
    _cooldown_strikes: int = field(default=0, init=False, repr=False)  # in a row
    _job_strikes: int = field(default=0, init=False, repr=False)  # in this job
    _paused_job: Job | None = field(default=None, init=False, repr=False)
    _cooldown_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    # Set when the whole job must stop (e.g. the disk is full); a sentence for the user.
    abort_reason: str | None = field(default=None, init=False)

    # --- building jobs -------------------------------------------------------------
    def build_job(self, text: str) -> Job:
        """Resolve one input (Spotify link or search text) into a Job. May raise."""
        text = text.strip()
        if is_spotify_input(text):
            collection = self.resolver.resolve(text)
            tasks = [
                TrackTask(track=track, position=position)
                for position, track in enumerate(collection.tracks, start=1)
            ]
            return Job(input=text, collection=collection, tasks=tasks)
        if self.query_lookup is None:
            raise ValueError("Plain search text is not supported here; pass a Spotify URL.")
        found = self.query_lookup(text)
        if found is None:
            raise LookupError(f"No YouTube Music results for {text!r}")
        track, match = found
        collection = Collection(kind="search", name=text, tracks=[track], source="ytmusic")
        return Job(
            input=text,
            collection=collection,
            tasks=[TrackTask(track=track, position=1, match=match)],
        )

    def job_from_tasks(
        self, text: str, collection: Collection | None, tasks: list[TrackTask]
    ) -> Job:
        return Job(input=text, collection=collection, tasks=tasks)

    # --- running ---------------------------------------------------------------------
    def resume(self) -> bool:
        """End a rate-limit pause early (the user pressed Try now). False if not paused."""
        with self._cooldown_lock:
            if self._cooldown_until <= time.monotonic():
                return False
            self._cooldown_until = 0.0
            self._cooldown_strikes = 0
        log.info("rate-limit pause ended early by the user")
        return True

    @property
    def output_dir(self) -> Path:
        return self.settings.resolved_output_dir

    def _clean_stale_tmp(self) -> Path:
        root = self.output_dir / TMP_DIR_NAME
        root.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - STALE_TMP_AGE_S
        for child in root.iterdir():
            try:
                if child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                pass
        return root

    async def run(self, job: Job) -> Job:
        """Run every task of ``job``; returns the same job with statuses filled in.

        Raises :class:`~tunegrab.preflight.JobAborted` if the music folder is unusable."""
        check_folder(self.output_dir)
        tmp_root = self._clean_stale_tmp()
        collection = job.collection
        if (
            collection is not None
            and collection.is_playlist_like
            and collection.id
            and collection.source != "retry"
        ):
            self.archive.set_playlist(collection)
        self._emit(job)
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(
            max_workers=self.settings.threads, thread_name_prefix="tunegrab"
        )
        # A playlist can list the same song twice: download it once, then let the repeats
        # find it in the archive ("already downloaded") instead of racing the first copy.
        seen: set[str] = set()
        first: list[TrackTask] = []
        repeats: list[TrackTask] = []
        for task in job.tasks:
            (repeats if task.track.key in seen else first).append(task)
            seen.add(task.track.key)
        try:
            for batch in (first, repeats):
                await asyncio.gather(
                    *(
                        loop.run_in_executor(executor, self._process, job, task, tmp_root)
                        for task in batch
                    )
                )
        except asyncio.CancelledError:
            self.cancel()
            job.cancelled = True
            raise
        finally:
            if self.cancel_event.is_set() and self.abort_reason is None:
                job.cancelled = True
            # Wait for the workers in a thread: blocking here would freeze the event loop
            # (and with it the web server) until every download in flight has stopped.
            await asyncio.shield(
                loop.run_in_executor(None, lambda: executor.shutdown(True, cancel_futures=True))
            )
            for task in job.tasks:
                if self.abort_reason is not None and (
                    not task.status.finished
                    or (task.status == TaskStatus.SKIPPED and task.message == "cancelled")
                ):
                    self._fail_task(job, task, f"Not downloaded: {self.abort_reason}")
                elif not task.status.finished:
                    task.status = TaskStatus.SKIPPED
                    task.message = "cancelled"
            if self.abort_reason is not None:
                job.error = self.abort_reason
            self._finish_playlist(job)
            with contextlib.suppress(OSError):  # only if empty: no stray folder in the library
                tmp_root.rmdir()
            job.finished_at = datetime.now(UTC)
            self._emit(job)
        return job

    # --- YouTube rate limits ------------------------------------------------------------
    def _rate_limited(self, job: Job | None = None) -> float:
        """YouTube said "slow down": pause every worker, longer each time it repeats; on
        the third time in a job pause for an hour, on the fifth stop the job."""
        with self._cooldown_lock:
            self._cooldown_strikes += 1
            self._job_strikes += 1
            if self._job_strikes >= RATE_LIMIT_GIVE_UP_AFTER:
                pause = 0.0
            elif self._job_strikes >= RATE_LIMIT_BLOCK_AFTER:
                pause = self.rate_limit_block
            else:
                pause = min(
                    RATE_LIMIT_MAX_PAUSE_S,
                    self.rate_limit_pause * 2.0 ** (self._cooldown_strikes - 1),
                )
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + pause)
            give_up = self._job_strikes >= RATE_LIMIT_GIVE_UP_AFTER
        if give_up:
            self.abort(RATE_LIMIT_GAVE_UP)
        elif job is not None and pause >= self.rate_limit_block > 0:
            job.paused_until = datetime.now(UTC) + timedelta(seconds=pause)
            self._emit(job)
        return pause

    def _wait_for_cooldown(self, job: Job, task: TrackTask) -> None:
        while True:
            with self._cooldown_lock:
                remaining = self._cooldown_until - time.monotonic()
            if remaining <= 0:
                if job.paused_until is not None:
                    job.paused_until = None
                    self._emit(job)
                return
            if job.paused_until is not None:
                at = job.paused_until.astimezone().strftime("%H:%M")
                task.message = f"YouTube paused downloads from your connection: continuing at {at}"
            else:
                task.message = f"YouTube asked to slow down: waiting {remaining:.0f} s"
            self._emit(job, task)
            if self.cancel_event.wait(min(remaining, 1.0)):  # short: Try now reacts fast
                raise Cancelled()

    def _finish_playlist(self, job: Job) -> None:
        collection = job.collection
        if (
            collection is None
            or not collection.is_playlist_like
            or not collection.id
            or self.settings.playlist_mode != "m3u"
        ):
            return
        try:
            self.write_playlist_file(collection.id)
        except OSError as exc:
            log.warning("Could not write playlist file for %s: %s", collection.name, exc)

    def write_playlist_file(self, playlist_id: str) -> Path | None:
        name = self.archive.playlist_name(playlist_id)
        if name is None:
            return None
        entries = []
        for _position, track in self.archive.playlist_members(playlist_id):
            path = self.archive.downloaded_path(track.key)
            if path is not None:
                entries.append((path, track))
        return write_m3u8(playlist_file(self.output_dir, name), name, entries)

    # --- one task (worker thread) ----------------------------------------------------
    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()

    def target_path(self, job: Job, task: TrackTask) -> Path:
        collection = job.collection
        ext = extension(self.settings.format)
        if (
            collection is not None
            and collection.is_playlist_like
            and self.settings.playlist_mode == "folder"
        ):
            fields = template_fields(
                task.track, ext, playlist=collection.name, position=task.position
            )
            return render_path(self.settings.playlist_template, fields, self.output_dir)
        return render_path(
            self.settings.track_template, template_fields(task.track, ext), self.output_dir
        )

    def _process(self, job: Job, task: TrackTask, tmp_root: Path) -> None:
        if self.cancel_event.is_set():
            self._set(job, task, TaskStatus.SKIPPED, "cancelled")
            return
        try:
            if self._skip_existing(job, task):
                return
            self._run_attempts(
                job,
                task,
                lambda: self._attempt(job, task, tmp_root),
                retryable=lambda exc: not isinstance(exc, TaskFailed) or exc.retryable,
            )
        except (Cancelled, DownloadCancelled):
            self._set(job, task, TaskStatus.SKIPPED, "cancelled")
        except Exception as exc:
            self._fail_with(job, task, exc)

    def _record_failure(self, job: Job, task: TrackTask) -> None:
        self.archive.record_failure(
            job.input,
            job.collection,
            task.position,
            task.track,
            task.failed_stage,
            task.error or "",
        )

    def _adopt_existing(self, job: Job, task: TrackTask) -> bool:
        """The song is already in the library under its usual name but missing from the
        download history (a reset history, or a record that failed to save): use that file
        instead of downloading it again as "Song (2)"."""
        if task.track.is_incomplete:
            return False  # its usual name isn't known until the details are filled in
        target = self.target_path(job, task)
        if not target.is_file() or self.archive.path_is_recorded(target):
            return False  # nothing there, or it's another song's file
        found = self.tagger.identify(target)
        if found is None or not _same_song(found, task.track):
            return False
        self.archive.record_download(task.track, target, None, self.settings.format)
        self.archive.resolve_failures(task.track.key)
        task.path = str(target)
        self._set(job, task, TaskStatus.SKIPPED, "already in your library")
        return True

    def _skip_existing(self, job: Job, task: TrackTask) -> bool:
        existing = self.archive.downloaded_path(task.track.key)
        if existing is None:
            return self._adopt_existing(job, task)
        if existing.suffix.lower() != "." + extension(self.settings.format):
            return False  # asked for another format: download it in that format too
        folder_mode = (
            job.collection is not None
            and job.collection.is_playlist_like
            and self.settings.playlist_mode == "folder"
        )
        if folder_mode:
            record = self.archive.get_download(task.track.key)
            track = record.track if record else task.track
            target = self.target_path(job, task.model_copy(update={"track": track}))
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(existing, target)
            task.path = str(target)
            self._set(job, task, TaskStatus.SKIPPED, "already downloaded (copied to playlist)")
            return True
        task.path = str(existing)
        self.archive.resolve_failures(task.track.key)
        self._set(job, task, TaskStatus.SKIPPED, "already downloaded")
        return True

    def _attempt(self, job: Job, task: TrackTask, tmp_root: Path) -> None:
        self._check_cancel()
        # 1. resolve: fill in album details the playlist/artist listing lacked
        if task.track.spotify_id and task.track.is_incomplete:
            self._set(job, task, TaskStatus.RESOLVING)
            task.track = self.resolver.enrich(task.track)

        # 2. match
        self._check_cancel()
        match = task.match
        if match is None or match.candidate is None:
            self._set(job, task, TaskStatus.MATCHING)
            match = self.matcher.match(task.track)
            task.match = match
        if match.candidate is None or not match.accepted(self.settings.match_threshold):
            best = f"best {match.score:.0f}: {match.reason}" if match.candidate else match.reason
            raise TaskFailed(
                f"no match above {self.settings.match_threshold:.0f} ({best})",
                retryable=False,
            )
        candidate = match.candidate

        # 3-4. download + convert into a private temp dir
        self._check_cancel()
        self._set(job, task, TaskStatus.DOWNLOADING)
        with tempfile.TemporaryDirectory(dir=tmp_root, prefix="task-") as tmp:

            def progress(fraction: float, phase: str) -> None:
                task.progress = fraction
                if phase == "converting" and task.status != TaskStatus.CONVERTING:
                    self._set(job, task, TaskStatus.CONVERTING)
                else:
                    self._emit(job, task)

            audio, candidate = self._download(job, task, candidate, Path(tmp), progress)

            # 5. tag (cover art + lyrics), still inside the temp dir
            self._check_cancel()
            self._set(job, task, TaskStatus.TAGGING)
            track = task.track
            cover = self._cover(track.cover_url or candidate.thumbnail_url)
            lyrics = self._lyrics(track)
            self.tagger.tag(audio, track, cover=cover, lyrics=lyrics.text if lyrics else None)

            # 6. move into the library
            self._check_cancel()
            target = move_into_place(audio, self.target_path(job, task))
        if lyrics is not None and lyrics.synced and self.settings.lrc:
            try:
                write_lrc(target, lyrics.synced)
            except OSError as exc:
                log.info("could not write .lrc for %s: %s", target.name, exc)

        # 7. archive (the file is safe in the library whatever happens here)
        task.path = str(target)
        task.progress = 1.0
        message = ""
        try:
            self.archive.record_download(track, target, candidate.video_id, self.settings.format)
            self.archive.resolve_failures(track.key)
        except Exception:
            log.exception("could not record %s in the download history", target)
            message = "saved, but not added to the download history"
        self._set(job, task, TaskStatus.DONE, message)

    def _download(
        self,
        job: Job,
        task: TrackTask,
        candidate: Candidate,
        dest: Path,
        progress: Callable[[float, str], None],
    ) -> tuple[Path, Candidate]:
        """Download ``candidate``; if YouTube says it's unavailable (removed, private,
        blocked in this country, age-gated) try the next-best match instead."""
        tried: set[str] = set()
        while True:
            if self.cancel_event.is_set():
                raise Cancelled()
            self._wait_for_cooldown(job, task)
            self._pace(job, task)
            if task.message and task.message.startswith(("Pacing", "YouTube")):
                task.message = ""
                self._emit(job, task)

            def on_source(info: SourceInfo) -> None:
                task.source = info.label
                task.low_quality = info.low_quality

            try:
                audio = self.downloader.download(
                    candidate.video_id,
                    dest,
                    progress=progress,
                    cancel=self.cancel_event,
                    source=on_source,
                )
            except DownloadCancelled:
                raise
            except DownloadError as exc:
                if exc.kind == "rate_limited":
                    # Not the song's fault: wait and try it again without using a retry.
                    pause = self._rate_limited(job)
                    log.warning("YouTube rate limit; pausing downloads for %.0f s", pause)
                    continue
                if exc.kind != "unavailable" or candidate.source == "manual":
                    raise
                tried.add(candidate.video_id)
                if len(tried) > MAX_ALTERNATIVES:
                    raise
                alternative = self.matcher.match(task.track, exclude=tried)
                if alternative.candidate is None or not alternative.accepted(
                    self.settings.match_threshold
                ):
                    raise TaskFailed(
                        f"{exc} (and no other match scored {self.settings.match_threshold:.0f}+)",
                        retryable=False,
                    ) from exc
                log.info(
                    "%s unavailable (%s); trying %s", candidate.video_id, exc, alternative.reason
                )
                task.match = alternative
                candidate = alternative.candidate
                task.message = "first choice unavailable: trying the next match"
                self._set(job, task, TaskStatus.DOWNLOADING, task.message)
                continue
            with self._cooldown_lock:
                self._cooldown_strikes = 0
            return audio, candidate

    def _cover(self, url: str | None) -> bytes | None:
        if not url or self.cover_fetcher is None:
            return None
        try:
            return self.cover_fetcher(url)
        except Exception as exc:
            log.info("cover art download failed (%s): %s", url, exc)
            return None

    def _lyrics(self, track: Track) -> Lyrics | None:
        if not self.settings.lyrics or self.lyrics is None:
            return None
        try:
            return self.lyrics.fetch(track)
        except Exception as exc:
            log.info("lyrics lookup failed for %s: %s", track.display, exc)
            return None


def _same_song(found: tuple[str, str], track: Track) -> bool:
    title, artist = (part.casefold().strip() for part in found)
    return title == track.title.casefold().strip() and (
        track.primary_artist.casefold().strip() in artist
    )


def run_job_sync(pipeline: Pipeline, job: Job) -> Job:
    """Run a job from synchronous code (CLI)."""
    return asyncio.run(pipeline.run(job))
