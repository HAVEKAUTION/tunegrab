"""FastAPI app: REST endpoints + Server-Sent Events, reusing pipeline.py.

Binds to 127.0.0.1 by default (see ``tunegrab serve``). There is no authentication,
so it must not be exposed to untrusted networks.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from tunegrab import __version__, storage
from tunegrab.config import (
    AudioFormat,
    Settings,
    VideoQuality,
    coerce_setting,
    config_file,
    log_dir,
    read_config_file,
    startup_warnings,
    write_config_file,
)
from tunegrab.doctor import Status, check_output_dir, run_all, update_ytdlp
from tunegrab.download.ytdlp import DownloadError
from tunegrab.errors import TuneGrabError
from tunegrab.imports import collection_from_csv
from tunegrab.library.paths import TemplateError, validate_template
from tunegrab.matching.base import SearchError
from tunegrab.metadata.base import MetadataError, NotFoundError
from tunegrab.models import Candidate, Collection, Job, Match, TaskStatus, Track, TrackTask
from tunegrab.net import NetworkError
from tunegrab.pipeline import Pipeline
from tunegrab.preflight import JobAborted
from tunegrab.runner import unexpected_message as unexpected_error
from tunegrab.services import Services, build_services
from tunegrab.urls import (
    InvalidURLError,
    SpotifyRef,
    is_spotify_input,
    is_video_link,
    parse_youtube_id,
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
    parse_time,
    probe,
)

log = logging.getLogger(__name__)

PROGRESS_EVENT_INTERVAL_S = 0.3
MAX_JOBS = 200  # in memory; older finished jobs stay in the archive's job history
SYSTEM_CACHE_S = 60.0
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})
HEARTBEAT_S = 15.0
SHUTDOWN_WAIT_S = 15.0  # how long a stopping server lets running songs finish cleanly


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
    * ``csv_text``: an Exportify-style CSV, downloaded as a playlist called ``name``.
    """

    url: str | None = None
    ytm_album: str | None = None
    spotify_tracks: list[str] | None = None
    csv_text: str | None = Field(default=None, max_length=20_000_000)
    track_ids: list[str] | None = None
    name: str | None = None
    format: AudioFormat | None = None

    @property
    def label(self) -> str:
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
        job = Job(input=request.label.strip())
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

    def _settings_for(self, fmt: AudioFormat | None) -> Settings:
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
        self, options: VideoOptions, entries: dict[str, VideoEntry], fmt: AudioFormat | None
    ) -> VideoPipeline:
        return VideoPipeline(self._settings_for(fmt), options, entries, on_event=self._on_event)

    async def submit_video(self, request: VideoRequest) -> Job:
        self.loop = asyncio.get_running_loop()
        job = Job(input=request.url.strip())
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
        if not records:
            raise ValueError("No failed tracks to retry.")
        tracks = list({r.key: r.track for r in records}.values())
        collection = Collection(kind="selection", name="Retry failed tracks", tracks=tracks)
        job = Job(
            input="retry",
            collection=collection,
            tasks=[TrackTask(track=t, position=i) for i, t in enumerate(tracks, start=1)],
        )
        self.loop = asyncio.get_running_loop()
        self.jobs[job.id] = job
        self._publish("job", job_summary(job))
        self._spawn(self._run(job, self.services.pipeline(on_event=self._on_event)))
        return job

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
        job = Job(input=old.input, collection=collection, tasks=tasks)
        self.jobs[job.id] = job
        self.loop = asyncio.get_running_loop()
        self._publish("job", job_summary(job))
        runner: Runner
        if video is not None:
            options, old_entries = video
            entries = {new.id: old_entries[o.id] for new, o in zip(tasks, failed, strict=True)}
            self.video_jobs[job.id] = (options, entries)
            runner = self._video_pipeline(options, entries, None)
        else:
            runner = self.services.pipeline(on_event=self._on_event)
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


# Settings the browser may change (secrets/paths to the archive stay CLI/.env only).
UI_SETTINGS = (
    "output_dir",
    "format",
    "bitrate",
    "threads",
    "lyrics",
    "lrc",
    "playlist_mode",
    "track_template",
    "playlist_template",
    "video_quality",
)


def settings_payload(settings: Settings) -> dict[str, Any]:
    values = {key: getattr(settings, key) for key in UI_SETTINGS}
    values["output_dir"] = str(settings.resolved_output_dir)
    return {"values": values, "saved": sorted(read_config_file())}


def check_music_folder(folder: Path) -> None:
    """Raise ValueError (shown in Settings) unless new files can be saved in ``folder``."""
    folder = folder.expanduser()
    if not folder.is_absolute():
        raise ValueError("Use a full folder path (Choose folder fills one in for you).")
    result = check_output_dir(folder)
    if result.status is Status.FAIL:
        raise ValueError(f"TuneGrab can't save music there: {result.detail}")


def apply_settings(current: Settings, changes: dict[str, Any]) -> Settings:
    """Validate ``changes``, save them to the config file and return updated settings."""
    unknown = sorted(set(changes) - set(UI_SETTINGS))
    if unknown:
        raise ValueError(f"Can't change {', '.join(unknown)} here")
    typed: dict[str, Any] = {}
    for key, raw in changes.items():
        value = coerce_setting(key, "" if raw is None else str(raw).strip())
        if key.endswith("_template") and value:
            validate_template(str(value))
        if key == "output_dir" and value:
            check_music_folder(Path(str(value)))
        typed[key] = value
    stored = read_config_file()
    update: dict[str, Any] = {}
    for key, value in typed.items():
        if value is None:
            stored.pop(key, None)
            update[key] = Settings.model_fields[key].default
        else:
            stored[key] = value
            update[key] = Path(value).expanduser() if key == "output_dir" else value
    write_config_file(stored)
    return current.model_copy(update=update)


def open_in_file_manager(path: Path) -> None:
    """Show ``path`` (a folder, or a file selected in its folder) in Explorer/Finder/etc."""
    if sys.platform == "win32":
        if path.is_file():
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            os.startfile(path)  # type: ignore[attr-defined,unused-ignore]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)] if path.is_file() else ["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path if path.is_dir() else path.parent)])


def pick_folder(initial: Path) -> str | None:
    """Native folder picker on the machine running the server (it's always local)."""
    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        chosen = filedialog.askdirectory(
            initialdir=str(initial if initial.exists() else Path.home()),
            title="Choose where TuneGrab saves music",
            parent=root,
        )
    finally:
        root.destroy()
    return chosen or None


class OpenRequest(BaseModel):
    path: str | None = None


class FixRequest(BaseModel):
    youtube_url: str


def manual_candidate(services: Services, track: Track, video_id: str) -> Candidate:
    """The chosen YouTube video as a match candidate; falls back to the track's own
    details when the video can't be looked up (it is still downloaded by ID)."""
    try:
        found = services.youtube.video(video_id)
    except SearchError as exc:
        log.info("video lookup failed for %s: %s", video_id, exc)
        return Candidate(
            video_id=video_id,
            title=track.title,
            artists=track.artists,
            duration_s=round(track.duration_s),
            source="manual",
        )
    return found.model_copy(update={"source": "manual"})


def host_name(host_header: str) -> str:
    """``127.0.0.1:8765`` -> ``127.0.0.1``; ``[::1]:8765`` -> ``[::1]``."""
    if host_header.startswith("["):
        return host_header.split("]", 1)[0] + "]"
    return host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header


def create_app(
    settings: Settings | None = None,
    services: Services | None = None,
    allowed_hosts: set[str] | None = None,
) -> FastAPI:
    owned = services is None
    services = services or build_services(settings or Settings())
    manager = JobManager(services)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        manager.loop = asyncio.get_running_loop()
        yield
        for pipeline in list(manager.pipelines.values()):
            pipeline.cancel()
        if manager._background:  # let workers stop and record what they did
            _done, pending = await asyncio.wait(manager._background, timeout=SHUTDOWN_WAIT_S)
            if pending:
                log.warning("%d job(s) still busy at shutdown", len(pending))
        if owned:
            services.close()

    app = FastAPI(title="TuneGrab", version=__version__, lifespan=lifespan)
    app.state.manager = manager
    hosts = set(LOCAL_HOSTS) | (allowed_hosts or set())

    @app.exception_handler(TuneGrabError)
    async def expected_error(request: Request, exc: TuneGrabError) -> JSONResponse:
        """An error an endpoint didn't catch itself but that has a user message."""
        status = 502 if isinstance(exc, NetworkError) else 400
        return JSONResponse({"detail": str(exc)}, status_code=status)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        """Last resort: log the traceback, show a plain sentence, never a stack trace."""
        log.error("unhandled error in %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse({"detail": unexpected_error(exc)}, status_code=500)

    @app.middleware("http")
    async def local_only(request: Request, call_next: Any) -> Response:
        """Block DNS rebinding (foreign Host) and cross-site requests (foreign Origin)."""
        host = request.headers.get("host", "")
        if host_name(host).lower() not in hosts:
            return JSONResponse({"detail": "Unknown host"}, status_code=403)
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin is not None:
            scheme_host = origin.split("://", 1)[-1]
            if scheme_host != host:
                return JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)
        response: Response = await call_next(request)
        return response

    system_cache: dict[str, Any] = {}

    @app.get("/api/system")
    async def system(fresh: bool = False) -> dict[str, Any]:
        """Health of the tools downloads depend on (doctor checks), cached briefly."""
        now = time.monotonic()
        if fresh or not system_cache or now - system_cache["at"] > SYSTEM_CACHE_S:
            results = await asyncio.to_thread(run_all, services.settings)
            system_cache.update(
                at=now,
                checks=[
                    {"name": r.name, "status": r.status.value, "detail": r.detail, "hint": r.hint}
                    for r in results
                ],
            )
        checks = system_cache["checks"]
        return {
            "version": __version__,
            "ok": all(c["status"] != "fail" for c in checks),
            "checks": checks,
            "paths": {
                "library": str(services.settings.resolved_output_dir),
                "archive": str(services.settings.resolved_archive_path),
                "log": str(log_dir() / "tunegrab.log"),
                "config": str(config_file()),
            },
            "default_folder": "output_dir" not in read_config_file(),
            "failures": len(services.archive.open_failures()),
            "warnings": list(startup_warnings),
        }

    @app.delete("/api/system/warnings")
    def dismiss_warnings() -> dict[str, Any]:
        startup_warnings.clear()
        return {"warnings": []}

    @app.get("/api/history")
    def history() -> list[dict[str, Any]]:
        live = set(manager.jobs)
        return [h for h in services.archive.job_history() if h.get("id") not in live]

    @app.delete("/api/jobs/finished")
    def clear_finished() -> dict[str, Any]:
        return {"cleared": manager.clear_finished()}

    update_lock = asyncio.Lock()

    @app.get("/api/storage")
    async def get_storage(uv: bool = False) -> dict[str, Any]:
        """What TuneGrab keeps on disk besides the music."""
        items = await asyncio.to_thread(storage.report, services.settings, uv)
        return {"items": [i.payload() for i in items]}

    @app.post("/api/storage/clean")
    async def post_clean(body: CleanRequest) -> dict[str, Any]:
        keys = list(dict.fromkeys(body.keys))
        known = {"tmp", "logs", "database", "ytdlp_cache", "uv_cache"}
        if not keys or not set(keys) <= known:
            raise HTTPException(400, f"Choose from: {', '.join(sorted(known))}")
        if "tmp" in keys and manager.busy():
            keys.remove("tmp")  # those folders are in use right now
        results = await asyncio.to_thread(storage.clean, services.settings, keys)
        return {
            "freed": sum(r.freed for r in results),
            "results": [{"key": r.key, "freed": r.freed, "detail": r.detail} for r in results],
        }

    @app.post("/api/system/update-ytdlp")
    async def post_update_ytdlp() -> dict[str, Any]:
        """Upgrade yt-dlp (YouTube changes often; an old yt-dlp is the usual cause of
        failures). The new version is used after TuneGrab restarts."""
        if manager.busy():
            raise HTTPException(409, "Wait for the downloads to finish, then update.")
        if update_lock.locked():
            raise HTTPException(409, "An update is already running.")
        async with update_lock:
            ok, output = await asyncio.to_thread(update_ytdlp)
        system_cache.clear()
        if not ok:
            raise HTTPException(500, f"Update failed: {output[-2000:]}")
        return {"ok": True, "output": output[-2000:]}

    @app.post("/api/failures/retry", status_code=202)
    async def retry_failures() -> dict[str, Any]:
        try:
            job = await manager.retry_failures()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return job_summary(job)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return files("tunegrab.web").joinpath("static/index.html").read_text(encoding="utf-8")

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "metadata": services.resolver.provider_names}

    @app.get("/api/preview")
    async def preview(url: str) -> dict[str, Any]:
        try:
            collection = await asyncio.to_thread(services.resolver.resolve, url)
        except InvalidURLError as exc:
            raise HTTPException(400, str(exc)) from exc
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except MetadataError as exc:
            raise HTTPException(502, str(exc)) from exc
        data = collection.model_dump(mode="json")
        return data

    @app.post("/api/video", status_code=202)
    async def create_video_job(request: VideoRequest) -> dict[str, Any]:
        """Download a video link (YouTube or any yt-dlp site) as MP4, a clip, or audio."""
        if not is_video_link(request.url):
            raise HTTPException(400, "That isn't a video link.")
        try:  # fail fast on bad times; they're checked against the length later
            start, end = parse_time(request.start), parse_time(request.end)
            check_clip(start, end, None)
        except VideoError as exc:
            raise HTTPException(400, str(exc)) from exc
        return job_summary(await manager.submit_video(request))

    @app.post("/api/jobs", status_code=202)
    async def create_job(request: JobRequest) -> dict[str, Any]:
        sources = [
            bool(request.url and request.url.strip()),
            bool(request.ytm_album),
            bool(request.spotify_tracks),
            bool(request.csv_text and request.csv_text.strip()),
        ]
        if sum(sources) != 1:
            raise HTTPException(400, "Give one of url, ytm_album, spotify_tracks or csv_text")
        if request.url and is_video_link(request.url) and not request.track_ids:
            # "Download now" on a YouTube (etc.) link: the video's audio, as music
            video = VideoRequest(url=request.url, mode="audio", format=request.format)
            return job_summary(await manager.submit_video(video))
        job = await manager.submit(request)
        return job_summary(job)

    def _browse_error(exc: Exception) -> HTTPException:
        if isinstance(exc, InvalidURLError):
            return HTTPException(400, str(exc))
        if isinstance(exc, NotFoundError):
            return HTTPException(404, str(exc))
        return HTTPException(502, str(exc))

    @app.get("/api/browse")
    async def browse(url: str) -> dict[str, Any]:
        """Artist -> its releases; album/playlist/track -> its tracks; plain text -> artist."""
        text = url.strip()
        if is_video_link(text):
            try:
                found = await asyncio.to_thread(manager.probe, text)
            except VideoError as exc:
                raise HTTPException(400, str(exc)) from exc
            except DownloadError as exc:
                raise HTTPException(502, str(exc)) from exc
            return {
                "type": "video",
                **found.payload(),
                "default_quality": services.settings.video_quality,
                "folder": str(services.settings.resolved_video_dir),
            }
        try:
            if not is_spotify_input(text):
                if not text:
                    raise InvalidURLError("Paste a Spotify link or type an artist name")
                overview = await asyncio.to_thread(services.browser.ytmusic.artist, text)
                return {"type": "artist", **overview.model_dump(mode="json")}
            ref = await asyncio.to_thread(services.resolver.parse, text)
            if ref.kind == "artist":
                overview = await asyncio.to_thread(services.browser.spotify_artist, ref.id)
                return {"type": "artist", **overview.model_dump(mode="json")}
            collection = await asyncio.to_thread(services.resolver.resolve_ref, ref)
        except (InvalidURLError, MetadataError, SearchError) as exc:
            raise _browse_error(exc) from exc
        return {"type": "collection", **release_payload(collection)}

    @app.get("/api/browse/ytmusic")
    async def browse_ytmusic(artist: str) -> dict[str, Any]:
        try:
            overview = await asyncio.to_thread(services.browser.ytmusic.artist, artist)
        except (MetadataError, SearchError) as exc:
            raise _browse_error(exc) from exc
        return {"type": "artist", **overview.model_dump(mode="json")}

    @app.get("/api/release")
    async def release(source: str, id: str) -> dict[str, Any]:
        if source not in ("spotify", "ytmusic"):
            raise HTTPException(400, "source must be spotify or ytmusic")
        try:
            if source == "ytmusic":
                collection, matches = await asyncio.to_thread(services.browser.ytmusic.album, id)
                return release_payload(collection, matches)
            collection = await asyncio.to_thread(
                services.resolver.resolve_ref, SpotifyRef("album", id)
            )
        except (MetadataError, SearchError) as exc:
            raise _browse_error(exc) from exc
        return release_payload(collection)

    @app.get("/api/jobs")
    def list_jobs() -> list[dict[str, Any]]:
        return [
            job_summary(j)
            for j in sorted(manager.jobs.values(), key=lambda j: j.created_at, reverse=True)
        ]

    def _job(job_id: str) -> Job:
        job = manager.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "No such job")
        return job

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        job = _job(job_id)
        return {**job_summary(job), "tasks": [task_payload(job, t) for t in job.tasks]}

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    async def retry_job(job_id: str) -> dict[str, Any]:
        _job(job_id)
        try:
            job = manager.retry(job_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return job_summary(job)

    @app.post("/api/jobs/{job_id}/tasks/{task_id}/fix", status_code=202)
    async def fix_task(job_id: str, task_id: str, body: FixRequest) -> dict[str, Any]:
        """Use a YouTube link the user picked for a failed song, remember it, retry."""
        job = _job(job_id)
        task = next((t for t in job.tasks if t.id == task_id), None)
        if task is None:
            raise HTTPException(404, "No such song in this download")
        if task.status != TaskStatus.FAILED:
            raise HTTPException(409, "Only failed songs can be given a link here")
        try:
            video_id = parse_youtube_id(body.youtube_url)
        except InvalidURLError as exc:
            raise HTTPException(400, str(exc)) from exc
        candidate = await asyncio.to_thread(manual_candidate, services, task.track, video_id)
        services.archive.put_match(task.track.key, candidate, 100.0, "manual override", manual=True)
        try:
            retried = manager.retry(job_id, {task_id})
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return job_summary(retried)

    @app.post("/api/jobs/{job_id}/resume")
    def resume_job(job_id: str) -> dict[str, Any]:
        job = _job(job_id)
        try:
            manager.resume(job_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return job_summary(job)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, Any]:
        job = _job(job_id)
        manager.cancel(job_id)
        return job_summary(job)

    @app.get("/api/library")
    def library() -> dict[str, Any]:
        records = services.archive.all_downloads()
        return {
            "root": str(services.settings.resolved_output_dir),
            "tracks": [
                {
                    "key": r.key,
                    "title": r.track.title,
                    "artists": r.track.artists,
                    "album": r.track.album,
                    "album_artist": r.track.album_artist or r.track.primary_artist,
                    "year": r.track.year,
                    "track_number": r.track.track_number,
                    "duration_s": round(r.track.duration_s),
                    "cover_url": r.track.cover_url,
                    "path": str(r.path),
                    "exists": r.exists,
                    "format": r.format,
                    "downloaded_at": r.downloaded_at,
                }
                for r in records
            ],
            "playlists": services.archive.playlists(),
        }

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return settings_payload(services.settings)

    @app.post("/api/settings")
    def post_settings(changes: dict[str, Any]) -> dict[str, Any]:
        try:
            services.settings = apply_settings(services.settings, changes)
        except (KeyError, ValueError, TemplateError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return settings_payload(services.settings)

    picker_open = threading.Lock()  # Tk allows one dialog at a time; a second one crashes

    @app.post("/api/pick-folder")
    async def post_pick_folder() -> dict[str, Any]:
        if not picker_open.acquire(blocking=False):
            raise HTTPException(
                409, "The folder window is already open. It may be behind another window."
            )
        try:
            chosen = await asyncio.to_thread(pick_folder, services.settings.resolved_output_dir)
        except Exception as exc:  # no display / no tkinter
            log.warning("folder picker failed: %s", exc)
            raise HTTPException(
                501, "The folder window couldn't open here. Type the full folder path instead."
            ) from exc
        finally:
            picker_open.release()
        return {"path": chosen}

    @app.post("/api/open")
    def post_open(body: OpenRequest) -> dict[str, Any]:
        root = services.settings.resolved_output_dir.resolve()
        target = Path(body.path).resolve() if body.path else root
        if target != root and root not in target.parents:
            raise HTTPException(400, "Only files inside the library can be opened")
        if not target.exists():
            if target == root:
                root.mkdir(parents=True, exist_ok=True)
            else:
                raise HTTPException(404, "File not found")
        open_in_file_manager(target)
        return {"opened": str(target)}

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        queue = manager.subscribe()

        async def stream() -> AsyncIterator[str]:
            try:
                yield "retry: 3000\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        yield await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_S)
                    except TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                manager.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
