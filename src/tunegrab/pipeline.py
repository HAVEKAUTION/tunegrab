"""Orchestrates a Job: resolve -> match -> download -> convert -> tag -> archive.

Blocking work (HTTP, yt-dlp, ffmpeg, mutagen) runs in a bounded thread pool driven
by asyncio. Audio is downloaded into a private temp dir under the output folder and
only moved into the library after tagging succeeds, so an interrupted run never
leaves half-written files in the library.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from tunegrab.config import Settings
from tunegrab.download.convert import extension
from tunegrab.download.ytdlp import DownloadCancelled, Downloader
from tunegrab.library.archive import Archive
from tunegrab.library.paths import render_path, template_fields, unique_path
from tunegrab.library.playlists import playlist_file, write_m3u8
from tunegrab.matching.matcher import Matcher
from tunegrab.metadata.resolver import Resolver
from tunegrab.models import Collection, Job, Match, TaskStatus, Track, TrackTask
from tunegrab.tagging.lyrics import Lyrics, write_lrc
from tunegrab.urls import is_spotify_input

log = logging.getLogger(__name__)

TMP_DIR_NAME = ".tunegrab-tmp"
STALE_TMP_AGE_S = 6 * 3600
RETRY_BACKOFF_S = 2.0

EventCallback = Callable[[Job, TrackTask | None], None]


class Tagger(Protocol):
    def tag(
        self, path: Path, track: Track, cover: bytes | None = None, lyrics: str | None = None
    ) -> None: ...


class LyricsSource(Protocol):
    def fetch(self, track: Track) -> Lyrics | None: ...


class QueryLookup(Protocol):
    """Turns a free-text search into (track metadata, chosen match)."""

    def __call__(self, query: str) -> tuple[Track, Match] | None: ...


class TaskFailed(Exception):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class Cancelled(Exception):
    pass


@dataclass
class Pipeline:
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
    cancel_event: threading.Event = field(default_factory=threading.Event)

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
    def cancel(self) -> None:
        self.cancel_event.set()

    @property
    def output_dir(self) -> Path:
        return self.settings.resolved_output_dir

    def _emit(self, job: Job, task: TrackTask | None = None) -> None:
        if self.on_event is not None:
            try:
                self.on_event(job, task)
            except Exception:  # a broken UI must never kill a download
                log.exception("event callback failed")

    def _set(self, job: Job, task: TrackTask, status: TaskStatus, message: str = "") -> None:
        task.status = status
        task.message = message
        self._emit(job, task)

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
        """Run every task of ``job``; returns the same job with statuses filled in."""
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
        futures = [
            loop.run_in_executor(executor, self._process, job, task, tmp_root) for task in job.tasks
        ]
        try:
            await asyncio.gather(*futures)
        except asyncio.CancelledError:
            self.cancel()
            job.cancelled = True
            raise
        finally:
            if self.cancel_event.is_set():
                job.cancelled = True
            executor.shutdown(wait=True, cancel_futures=True)
            for task in job.tasks:
                if not task.status.finished:
                    task.status = TaskStatus.SKIPPED
                    task.message = "cancelled"
            self._finish_playlist(job)
            job.finished_at = datetime.now(UTC)
            self._emit(job)
        return job

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
            attempts = self.settings.retries + 1
            for attempt in range(1, attempts + 1):
                task.attempts = attempt
                try:
                    self._attempt(job, task, tmp_root)
                    return
                except TaskFailed as exc:
                    if not exc.retryable or attempt == attempts:
                        raise
                    log.info("retrying %s after: %s", task.track.display, exc)
                except (Cancelled, DownloadCancelled):
                    raise
                except Exception as exc:
                    if attempt == attempts:
                        raise TaskFailed(str(exc) or type(exc).__name__) from exc
                    log.info("retrying %s after: %s", task.track.display, exc)
                task.message = f"retry {attempt}/{attempts - 1}"
                self._emit(job, task)
                if self.cancel_event.wait(self.retry_backoff * attempt):
                    raise Cancelled()
        except (Cancelled, DownloadCancelled):
            self._set(job, task, TaskStatus.SKIPPED, "cancelled")
        except Exception as exc:
            stage = task.status.value if not task.status.finished else None
            task.failed_stage = stage
            task.error = str(exc) or type(exc).__name__
            log.warning("failed: %s at %s: %s", task.track.display, stage, task.error)
            try:
                self.archive.record_failure(
                    job.input, job.collection, task.position, task.track, stage, task.error
                )
            except Exception:
                log.exception("could not record failure")
            self._set(job, task, TaskStatus.FAILED, task.error)

    def _skip_existing(self, job: Job, task: TrackTask) -> bool:
        existing = self.archive.downloaded_path(task.track.key)
        if existing is None:
            return False
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
                f"no match above {self.settings.match_threshold:.0f} ({best}). "
                "Use `tunegrab fix` to choose one.",
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

            audio = self.downloader.download(
                candidate.video_id, Path(tmp), progress=progress, cancel=self.cancel_event
            )

            # 5. tag (cover art + lyrics), still inside the temp dir
            self._check_cancel()
            self._set(job, task, TaskStatus.TAGGING)
            track = task.track
            cover = self._cover(track.cover_url or candidate.thumbnail_url)
            lyrics = self._lyrics(track)
            self.tagger.tag(audio, track, cover=cover, lyrics=lyrics.text if lyrics else None)

            # 6. move into the library
            self._check_cancel()
            target = unique_path(self.target_path(job, task))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(audio), str(target))
        if lyrics is not None and lyrics.synced and self.settings.lrc:
            try:
                write_lrc(target, lyrics.synced)
            except OSError as exc:
                log.info("could not write .lrc for %s: %s", target.name, exc)

        # 7. archive
        task.path = str(target)
        task.progress = 1.0
        self.archive.record_download(track, target, candidate.video_id, self.settings.format)
        self.archive.resolve_failures(track.key)
        self._set(job, task, TaskStatus.DONE)

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


def run_job_sync(pipeline: Pipeline, job: Job) -> Job:
    """Run a job from synchronous code (CLI)."""
    return asyncio.run(pipeline.run(job))
