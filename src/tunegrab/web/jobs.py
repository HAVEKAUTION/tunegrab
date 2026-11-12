"""Jobs held by the web app: running them, their state, and the live event stream."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from typing import Any, Protocol

from pydantic import BaseModel, Field

from tunegrab.browse import SongHit, song_match
from tunegrab.config import (
    AudioFormat,
    Settings,
    VideoQuality,
)
from tunegrab.download.ytdlp import DownloadError
from tunegrab.imports import collection_from_csv
from tunegrab.library.archive import VideoRecord
from tunegrab.matching.base import SearchError
from tunegrab.metadata.base import MetadataError
from tunegrab.models import Collection, Job, Match, TaskStatus, Track, TrackTask
from tunegrab.pipeline import Pipeline
from tunegrab.preflight import JobAborted
from tunegrab.runner import unexpected_message as unexpected_error
from tunegrab.services import Services
from tunegrab.urls import (
    InvalidURLError,
    SpotifyRef,
)
from tunegrab.video import (
    VideoEntry,
    VideoError,
    VideoMode,
    VideoOptions,
    VideoPipeline,
    VideoProbe,
    build_job,
    check_clip,
    job_for_entries,
    parse_time,
    probe,
)

log = logging.getLogger(__name__)

PROGRESS_EVENT_INTERVAL_S = 0.3
MAX_JOBS = 200  # in memory; older finished jobs stay in the archive's job history


class VideoRequest(BaseModel):
    """A video link to download as video (MP4) or audio, optionally only a clip."""

    url: str = Field(min_length=1, max_length=4000)
    mode: VideoMode = "video"
    quality: VideoQuality | None = None  # default: the video_quality setting
    start: str | None = None  # "1:23"
    end: str | None = None
    format: AudioFormat | None = None  # for mode="audio"


class CleanRequest(BaseModel):
    keys: list[str] = Field(default_factory=lambda: ["tmp", "logs", "database", "ytdlp_cache"])


class Runner(Protocol):
    async def run(self, job: Job) -> Job: ...
    def cancel(self) -> None: ...


PROBE_CACHE_S = 600.0
PROBE_CACHE_SIZE = 32


class JobRequest(BaseModel):
    """One download job. Give exactly one source:

    * ``url``: a Spotify link or search text (optionally only ``track_ids`` of it);
    * ``ytm_album``: a YouTube Music album browse ID (optionally only ``track_ids``,
      which are video IDs);
    * ``spotify_tracks``: Spotify track IDs, downloaded as one job called ``name``;
    * ``csv_text``: an Exportify-style CSV, downloaded as a playlist called ``name``;
    * ``ytm_songs``: songs picked from YouTube Music search (their videos are known).
    """

    url: str | None = None
    ytm_album: str | None = None
    spotify_tracks: list[str] | None = None
    csv_text: str | None = Field(default=None, max_length=20_000_000)
    ytm_songs: list[SongHit] | None = Field(default=None, max_length=500)
    track_ids: list[str] | None = None
    name: str | None = None
    format: AudioFormat | None = None

    @property
    def label(self) -> str:
        if self.ytm_songs and not self.name:
            return self.ytm_songs[0].title if len(self.ytm_songs) == 1 else "Selected songs"
        return self.name or self.url or self.ytm_album or "Selected tracks"


def release_payload(
    collection: Collection, matches: dict[str, Match] | None = None
) -> dict[str, Any]:
    """A collection for the browser, each track with the ``id`` used for ``track_ids``."""
    data = collection.model_dump(mode="json")
    keys = list(matches or {})
    for index, track in enumerate(data["tracks"]):
        track["id"] = track.get("spotify_id") or (keys[index] if index < len(keys) else None)
    return data


def job_summary(job: Job) -> dict[str, Any]:
    collection = job.collection
    return {
        "id": job.id,
        "input": job.input,
        "name": job.name,
        "kind": collection.kind if collection else None,
        "source": collection.source if collection else None,
        "warnings": collection.warnings if collection else [],
        "total": len(job.tasks),
        "counts": job.counts(),
        "retryable": len(job.retryable()) if job.finished_at else 0,
        "created_at": job.created_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "cancelled": job.cancelled,
        "error": job.error,
        "state": job_state(job),
        "paused_until": job.paused_until.isoformat() if job.paused_until else None,
    }


def job_state(job: Job) -> str:
    if job.error:
        return "error"
    if job.collection is None:
        return "resolving"
    if job.finished_at:
        return "cancelled" if job.cancelled else "finished"
    if any(t.status != TaskStatus.QUEUED for t in job.tasks):
        return "running"
    return "queued"


# Every song snapshot gets a rising number so the page can drop one that arrives late
# (a job reload can be answered after a newer live event for the same song).
_revisions = itertools.count(1)


def task_payload(job: Job, task: TrackTask) -> dict[str, Any]:
    match = task.match
    return {
        "rev": next(_revisions),
        "job_id": job.id,
        "id": task.id,
        "position": task.position,
        "title": task.track.title,
        "artists": task.track.artists,
        "album": task.track.album,
        "duration_s": round(task.track.duration_s),
        "status": task.status.value,
        "progress": round(task.progress, 3),
        "message": task.message,
        "error": task.error,
        "path": task.path,
        "source": task.source,
        "low_quality": task.low_quality,
        "retried": task.id in job.retried,
        "match": (
            {
                "url": match.candidate.url,
                "title": match.candidate.title,
                "score": round(match.score),
                "reason": match.reason,
            }
            if match is not None and match.candidate is not None
            else None
        ),
    }


MAX_QUEUED_EVENTS = 1000
RESYNC_EVENT = "event: resync\ndata: {}\n\n"


class JobManager:
    """Holds jobs in memory, runs them one at a time (each job is parallel inside)."""

    def __init__(self, services: Services) -> None:
        self.services = services
        self.jobs: dict[str, Job] = {}
        self.pipelines: dict[str, Runner] = {}
        # video jobs: what's needed to retry them (options, task id -> video)
        self.video_jobs: dict[str, tuple[VideoOptions, dict[str, VideoEntry]]] = {}
        self._probes: dict[str, tuple[float, VideoProbe]] = {}
        self.subscribers: set[asyncio.Queue[str]] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._run_lock = asyncio.Lock()
        self._last_sent: dict[str, tuple[str, float]] = {}
        self._background: set[asyncio.Task[None]] = set()

    # -- events
    def _publish(self, event: str, data: dict[str, Any]) -> None:
        message = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        for queue in list(self.subscribers):
            if queue.qsize() >= MAX_QUEUED_EVENTS:
                # The page isn't keeping up: drop the backlog and tell it to reload instead.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(RESYNC_EVENT)
            queue.put_nowait(message)

    def _on_event(self, job: Job, task: TrackTask | None) -> None:
        """Called from pipeline worker threads."""
        if self.loop is None:
            return
        if task is None:
            payload = job_summary(job)
            self.loop.call_soon_threadsafe(self._publish, "job", payload)
            return
        now = time.monotonic()
        last = self._last_sent.get(task.id)
        if (
            last is not None
            and last[0] == task.status.value
            and not task.status.finished
            and now - last[1] < PROGRESS_EVENT_INTERVAL_S
        ):
            return
        self._last_sent[task.id] = (task.status.value, now)
        task_data = task_payload(job, task)
        summary = job_summary(job)
        self.loop.call_soon_threadsafe(self._publish, "task", task_data)
        if task.status.finished:
            self.loop.call_soon_threadsafe(self._publish, "job", summary)

    def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue()
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self.subscribers.discard(queue)

    # -- jobs
    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        task.add_done_callback(self._log_crash)

    @staticmethod
    def _log_crash(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            log.error("background job task crashed", exc_info=task.exception())

    def _fail(self, job: Job, message: str) -> None:
        """End ``job`` with an error: nothing it holds stays queued or running."""
        job.error = message
        for task in job.tasks:
            if not task.status.finished:
                task.status = TaskStatus.SKIPPED
                task.message = "stopped"
        job.finished_at = job.finished_at or job.created_at
        self._publish("job", job_summary(job))

    async def submit(self, request: JobRequest) -> Job:
        self.loop = asyncio.get_running_loop()
        job = Job(input=request.label.strip(), format=request.format)
        self.jobs[job.id] = job
        self._publish("job", job_summary(job))
        self._spawn(self._resolve_and_run(job, request))
        return job

    def _build(self, pipeline: Pipeline, request: JobRequest) -> Job:
        wanted = set(request.track_ids) if request.track_ids else None
        if request.ytm_album:
            collection, matches = self.services.browser.ytmusic.album(request.ytm_album)
            tasks = [
                TrackTask(track=match.track, position=position, match=match)
                for position, (video_id, match) in enumerate(matches.items(), start=1)
                if wanted is None or video_id in wanted
            ]
            collection.tracks = [t.track for t in tasks]
            return Job(input=request.label, collection=collection, tasks=tasks)
        if request.ytm_songs:
            picked = [song_match(hit) for hit in {h.id: h for h in request.ytm_songs}.values()]
            tasks = [
                TrackTask(track=m.track, position=i, match=m) for i, m in enumerate(picked, start=1)
            ]
            collection = Collection(
                kind="selection",
                name=request.label,
                tracks=[t.track for t in tasks],
                source="ytmusic",
            )
            return Job(input=request.label, collection=collection, tasks=tasks)
        if request.csv_text:
            collection = collection_from_csv(
                request.csv_text,
                (request.name or "Imported songs").strip() or "Imported songs",
                lookup=lambda tid: self.services.resolver.resolve_ref(
                    SpotifyRef("track", tid)
                ).tracks[0],
            )
            tasks = [
                TrackTask(track=t, position=i) for i, t in enumerate(collection.tracks, start=1)
            ]
            return Job(input=request.label, collection=collection, tasks=tasks)
        if request.spotify_tracks:
            tracks = [
                self.services.resolver.resolve_ref(SpotifyRef("track", tid)).tracks[0]
                for tid in dict.fromkeys(request.spotify_tracks)
            ]
            collection = Collection(
                kind="selection", name=request.name or "Selected tracks", tracks=tracks
            )
            tasks = [TrackTask(track=t, position=i) for i, t in enumerate(tracks, start=1)]
            return Job(input=request.label, collection=collection, tasks=tasks)
        job = pipeline.build_job(request.url or "")
        if wanted is not None and job.collection is not None:
            job.tasks = [t for t in job.tasks if t.track.spotify_id in wanted]
            job.collection.tracks = [t.track for t in job.tasks]
            if job.collection.kind == "artist":
                job.collection.kind = "selection"
        return job

    def _settings_for(self, fmt: str | None) -> Settings:
        if fmt is None:
            return self.services.settings
        return self.services.settings.model_copy(update={"format": fmt})

    async def _resolve_and_run(self, job: Job, request: JobRequest) -> None:
        try:
            pipeline = self.services.pipeline(
                on_event=self._on_event, settings=self._settings_for(request.format)
            )
            built = await asyncio.to_thread(self._build, pipeline, request)
            if not built.tasks:
                raise LookupError("None of the selected tracks were found.")
        except (KeyError, IndexError) as exc:  # LookupError subclasses that are bugs, not input
            log.exception("could not resolve %r", job.input)
            self._fail(job, unexpected_error(exc))
            return
        except (MetadataError, InvalidURLError, LookupError, ValueError, SearchError) as exc:
            self._fail(job, str(exc))
            return
        except Exception as exc:  # a job must always end, never sit in "resolving"
            log.exception("could not resolve %r", job.input)
            self._fail(job, unexpected_error(exc))
            return
        job.collection = built.collection
        job.tasks = built.tasks
        self._publish("job", job_summary(job))
        await self._run(job, pipeline)

    # -- video
    def probe(self, url: str) -> VideoProbe:
        """yt-dlp lookup, cached for a few minutes (Browse, then Download, = one lookup)."""
        key = url.strip()
        now = time.monotonic()
        cached = self._probes.get(key)
        if cached is not None and now - cached[0] < PROBE_CACHE_S:
            return cached[1]
        found = probe(key, self.services.settings.cookies_from_browser)
        self._probes[key] = (now, found)
        if len(self._probes) > PROBE_CACHE_SIZE:
            oldest = min(self._probes, key=lambda k: self._probes[k][0])
            self._probes.pop(oldest, None)
        return found

    def _video_pipeline(
        self, options: VideoOptions, entries: dict[str, VideoEntry], fmt: str | None
    ) -> VideoPipeline:
        return VideoPipeline(
            self._settings_for(fmt),
            options,
            entries,
            on_event=self._on_event,
            archive=self.services.archive,
        )

    async def submit_video(self, request: VideoRequest) -> Job:
        self.loop = asyncio.get_running_loop()
        job = Job(input=request.url.strip(), format=request.format)
        self.jobs[job.id] = job
        self._publish("job", job_summary(job))
        self._spawn(self._probe_and_run(job, request))
        return job

    async def _probe_and_run(self, job: Job, request: VideoRequest) -> None:
        try:
            found = await asyncio.to_thread(self.probe, request.url)
            start, end = parse_time(request.start), parse_time(request.end)
            if (start is not None or end is not None) and found.kind != "video":
                raise VideoError("A clip needs a single video, not a playlist.")
            duration = found.entries[0].duration_s if found.kind == "video" else None
            start, end = check_clip(start, end, duration)
            options = VideoOptions(
                mode=request.mode,
                quality=request.quality or self.services.settings.video_quality,
                start=start,
                end=end,
            )
            built, entries = build_job(found, options)
        except (VideoError, DownloadError) as exc:
            self._fail(job, str(exc))
            return
        except Exception as exc:  # a job must always end, never sit in "resolving"
            log.exception("could not look up video %r", request.url)
            self._fail(job, unexpected_error(exc))
            return
        job.collection, job.tasks = built.collection, built.tasks
        self.video_jobs[job.id] = (options, entries)
        self._publish("job", job_summary(job))
        await self._run(job, self._video_pipeline(options, entries, request.format))

    async def _run(self, job: Job, pipeline: Runner) -> None:
        self.pipelines[job.id] = pipeline
        async with self._run_lock:
            if job.cancelled:
                job.finished_at = job.created_at
                self._publish("job", job_summary(job))
                self._remember(job)
                return
            try:
                await pipeline.run(job)
            except JobAborted as exc:
                self._fail(job, str(exc))
            except Exception as exc:  # never let one job break the manager
                log.exception("job %s crashed", job.id)
                self._fail(job, unexpected_error(exc))
            finally:
                self.pipelines.pop(job.id, None)
                self._remember(job)

    def _remember(self, job: Job) -> None:
        """Persist the finished job's summary and keep the in-memory list bounded."""
        try:
            self.services.archive.save_job(job_summary(job))
        except Exception:
            log.exception("could not save job history for %s", job.id)
        finished = [j for j in self.jobs.values() if j.finished_at or j.error]
        excess = max(0, len(self.jobs) - MAX_JOBS)
        for old in sorted(finished, key=lambda j: j.created_at)[:excess]:
            self.jobs.pop(old.id, None)

    def busy(self) -> bool:
        return any(
            job_state(job) in ("resolving", "queued", "running") for job in self.jobs.values()
        )

    def clear_finished(self) -> int:
        done = [jid for jid, j in self.jobs.items() if j.finished_at or j.error]
        for jid in done:
            self.jobs.pop(jid, None)
        return len(done) + self.services.archive.clear_job_history()

    async def retry_failures(self) -> Job:
        """One job for every track whose last attempt failed (survives restarts)."""
        if any(j.input == "retry" and not (j.finished_at or j.error) for j in self.jobs.values()):
            raise ValueError("Failed songs are already being retried.")
        records = self.services.archive.open_failures()
        videos = self.services.archive.video_failures()
        if not records and not videos:
            raise ValueError("No failed tracks to retry.")
        # one job per format the songs were wanted in (usually just one)
        by_format: dict[str | None, dict[str, Track]] = {}
        for record in records:
            by_format.setdefault(record.format, {})[record.key] = record.track
        self.loop = asyncio.get_running_loop()
        jobs: list[Job] = []
        for fmt, found in by_format.items():
            tracks = list(found.values())
            name = "Retry failed tracks"
            if len(by_format) > 1:
                name += f" ({(fmt or self.services.settings.format).upper()})"
            job = Job(
                input="retry",
                format=fmt,
                collection=Collection(kind="selection", name=name, tracks=tracks),
                tasks=[TrackTask(track=t, position=i) for i, t in enumerate(tracks, start=1)],
            )
            self.jobs[job.id] = job
            self._publish("job", job_summary(job))
            pipeline = self.services.pipeline(
                on_event=self._on_event, settings=self._settings_for(fmt)
            )
            self._spawn(self._run(job, pipeline))
            jobs.append(job)
        jobs += self._retry_failed_videos(videos)
        return jobs[0]

    def _retry_failed_videos(self, records: list[VideoRecord]) -> list[Job]:
        """One job per set of options (and format) the failed videos were wanted with."""
        groups: dict[tuple[str, str | None], list[VideoRecord]] = {}
        for record in records:
            groups.setdefault(
                (json.dumps(record.options, sort_keys=True), record.format), []
            ).append(record)
        jobs: list[Job] = []
        for (options_json, fmt), items in groups.items():
            try:
                options = VideoOptions(**json.loads(options_json))
                videos = [VideoEntry(**r.entry) for r in items]
            except (TypeError, ValueError):
                log.warning("skipping unreadable video failures: %s", [r.key for r in items])
                continue
            name = "Retry failed videos" if options.mode == "video" else "Retry failed audio"
            job, entries = job_for_entries(items[0].url, name, None, videos, options)
            job.input, job.format = "retry", fmt
            self.jobs[job.id] = job
            self.video_jobs[job.id] = (options, entries)
            self._publish("job", job_summary(job))
            self._spawn(self._run(job, self._video_pipeline(options, entries, fmt)))
            jobs.append(job)
        return jobs

    def retry(self, job_id: str, task_ids: set[str] | None = None) -> Job:
        """Re-run the failed tracks of a job (or only ``task_ids`` of them)."""
        old = self.jobs[job_id]
        if not old.finished_at:
            raise ValueError("Wait for this download to finish first.")
        failed = [t for t in old.retryable() if task_ids is None or t.id in task_ids]
        if not failed:
            if any(t.id in old.retried for t in old.tasks if task_ids is None or t.id in task_ids):
                raise ValueError("Already retried; see the newer download.")
            raise ValueError("This job has no failed tracks.")
        old.retried.update(t.id for t in failed)
        self._publish("job", job_summary(old))
        tasks = [TrackTask(track=t.track, position=t.position) for t in failed]
        video = self.video_jobs.get(job_id)
        collection = old.collection
        if collection is not None and video is None:  # video jobs keep "video:<mode>"
            collection = collection.model_copy(update={"source": "retry"})
        job = Job(input=old.input, collection=collection, tasks=tasks, format=old.format)
        self.jobs[job.id] = job
        self.loop = asyncio.get_running_loop()
        self._publish("job", job_summary(job))
        runner: Runner
        if video is not None:
            options, old_entries = video
            entries = {new.id: old_entries[o.id] for new, o in zip(tasks, failed, strict=True)}
            self.video_jobs[job.id] = (options, entries)
            runner = self._video_pipeline(options, entries, old.format)
        else:
            runner = self.services.pipeline(
                on_event=self._on_event, settings=self._settings_for(old.format)
            )
        self._spawn(self._run(job, runner))
        return job

    def resume(self, job_id: str) -> None:
        """End a YouTube rate-limit pause early."""
        pipeline = self.pipelines.get(job_id)
        if not isinstance(pipeline, Pipeline) or not pipeline.resume():
            raise ValueError("This download isn't paused.")
        self.jobs[job_id].paused_until = None

    def cancel(self, job_id: str) -> None:
        job = self.jobs[job_id]
        job.cancelled = True
        pipeline = self.pipelines.get(job_id)
        if pipeline is not None:
            pipeline.cancel()
