"""Jobs held by the web app: running them, their state, and the live event stream."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

from pydantic import BaseModel, Field

from tunegrab.browse import SongHit, song_match
from tunegrab.config import (
    AudioFormat,
    Settings,
    VideoQuality,
)
from tunegrab.download.ytdlp import DownloadError
from tunegrab.imports import collection_from_csv, read_rows
from tunegrab.library.archive import DownloadRecord, VideoRecord
from tunegrab.matching.base import SearchError
from tunegrab.metadata.base import MetadataError
from tunegrab.models import (
    Alternative,
    Candidate,
    Collection,
    Job,
    Match,
    TaskStatus,
    Track,
    TrackTask,
)
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
LOOKUP_WORKERS = 6  # Spotify track lookups at once when a selection or CSV names songs by ID


def lookup_all(ids: list[str], lookup: Callable[[str], Track]) -> dict[str, Track]:
    """Look up several Spotify track IDs at once; IDs that fail are left out."""

    def one(track_id: str) -> tuple[str, Track | None]:
        try:
            return track_id, lookup(track_id)
        except (MetadataError, LookupError, IndexError) as exc:
            log.info("could not look up %s: %s", track_id, exc)
            return track_id, None

    unique = list(dict.fromkeys(ids))
    if len(unique) <= 1:
        found = [one(i) for i in unique]
    else:
        with ThreadPoolExecutor(min(LOOKUP_WORKERS, len(unique))) as pool:
            found = list(pool.map(one, unique))
    return {i: t for i, t in found if t is not None}


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


def candidate_payload(candidate: Candidate, score: float, reason: str) -> dict[str, Any]:
    """A YouTube video as the page shows it in a match: what, whose, how long, how sure."""
    return {
        "video_id": candidate.video_id,
        "url": candidate.url,
        "title": candidate.title,
        "channel": ", ".join(candidate.artists),
        "duration_s": candidate.duration_s,
        "score": round(score),
        "reason": reason,
    }


def review_payload(
    note: str | None, current: dict[str, Any] | None, alternatives: list[Alternative]
) -> dict[str, Any] | None:
    """What "Check this match" shows: why, the saved video and the other candidates."""
    if not note:
        return None
    return {
        "note": note,
        "match": current,
        "alternatives": [candidate_payload(a.candidate, a.score, a.reason) for a in alternatives],
    }


def task_payload(job: Job, task: TrackTask) -> dict[str, Any]:
    match = task.match
    current = (
        candidate_payload(match.candidate, match.score, match.reason)
        if match is not None and match.candidate is not None
        else None
    )
    return {
        "rev": next(_revisions),
        "job_id": job.id,
        "id": task.id,
        "key": task.track.key,
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
        "match": current,
        "review": review_payload(task.review, current, match.alternatives if match else []),
    }


def record_format(record: DownloadRecord) -> str | None:
    """The audio format a saved song is in (from its record, else its file name)."""
    suffix = record.path.suffix.lstrip(".").lower()
    return record.format or (suffix if suffix in ("mp3", "m4a", "opus", "flac") else None)


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
        self.loop.call_soon_threadsafe(self._publish, "task", task_data)
        if task.status.finished:  # counts change only when a song ends
            self.loop.call_soon_threadsafe(self._publish, "job", job_summary(job))
        if task.replaces is not None and task.status == TaskStatus.DONE:
            # the song has the video the user chose: earlier rows stop asking about it
            self.loop.call_soon_threadsafe(self._reviewed, task.track.key)

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

    async def submit(self, request: JobRequest, built: Job | None = None) -> Job:
        """Queue a job; ``built`` is one the caller already resolved (its name is known)."""
        self.loop = asyncio.get_running_loop()
        job = Job(input=request.label.strip(), format=request.format)
        if built is not None:
            job.collection, job.tasks = built.collection, built.tasks
        self.jobs[job.id] = job
        self._publish("job", job_summary(job))
        self._spawn(self._resolve_and_run(job, request, built))
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
            ids = [e for e in read_rows(request.csv_text) if isinstance(e, str)]
            found = lookup_all(ids, self._track)
            collection = collection_from_csv(
                request.csv_text,
                (request.name or "Imported songs").strip() or "Imported songs",
                lookup=lambda tid: found[tid],  # KeyError -> counted as skipped
            )
            tasks = [
                TrackTask(track=t, position=i) for i, t in enumerate(collection.tracks, start=1)
            ]
            return Job(input=request.label, collection=collection, tasks=tasks)
        if request.spotify_tracks:
            found = lookup_all(request.spotify_tracks, self._track)
            tracks = [found[tid] for tid in dict.fromkeys(request.spotify_tracks) if tid in found]
            missed = len(dict.fromkeys(request.spotify_tracks)) - len(tracks)
            collection = Collection(
                kind="selection",
                name=request.name or "Selected tracks",
                tracks=tracks,
                warnings=[f"{missed} of the selected songs couldn't be found."] if missed else [],
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

    def _track(self, track_id: str) -> Track:
        return self.services.resolver.resolve_ref(SpotifyRef("track", track_id)).tracks[0]

    def _forget(self, job_id: str) -> None:
        """Drop a job and everything kept for it in memory."""
        job = self.jobs.pop(job_id, None)
        self.pipelines.pop(job_id, None)
        self.video_jobs.pop(job_id, None)
        for task in job.tasks if job else []:
            self._last_sent.pop(task.id, None)

    def _settings_for(self, fmt: str | None) -> Settings:
        if fmt is None:
            return self.services.settings
        return self.services.settings.model_copy(update={"format": fmt})

    async def _resolve_and_run(
        self, job: Job, request: JobRequest, built: Job | None = None
    ) -> None:
        try:
            pipeline = self.services.pipeline(
                on_event=self._on_event, settings=self._settings_for(request.format)
            )
            if built is None:
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
            self._forget(old.id)

    def busy(self) -> bool:
        return any(
            job_state(job) in ("resolving", "queued", "running") for job in self.jobs.values()
        )

    def clear_finished(self) -> int:
        done = [jid for jid, j in self.jobs.items() if j.finished_at or j.error]
        for jid in done:
            self._forget(jid)
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

    # -- match review
    def _reviewed(self, key: str) -> None:
        """Drop "Check this match" from every song row in memory for ``key``."""
        for job in self.jobs.values():
            for task in job.tasks:
                if task.review and task.track.key == key:
                    task.review = None
                    self._publish("task", task_payload(job, task))

    def keep_match(self, key: str) -> None:
        """The user checked the match and it's right: stop flagging it, never rematch it."""
        archive = self.services.archive
        if archive.get_download(key) is None:
            raise LookupError("That song isn't in your library any more.")
        archive.clear_review(key)
        archive.confirm_match(key)
        self._reviewed(key)

    def choose_match(self, key: str, video_id: str) -> Job:
        """Download another candidate for a saved song; it takes the old file's place once
        it is tagged and ready, and is remembered as the song's video."""
        archive = self.services.archive
        record = archive.get_download(key)
        if record is None:
            raise LookupError("That song isn't in your library any more.")
        saved = archive.match_record(key)
        options = saved.alternatives if saved is not None else []
        picked = next((a for a in options if a.candidate.video_id == video_id), None)
        if saved is None or picked is None:
            raise ValueError("That video isn't one of the other matches saved for this song.")
        for job in self.jobs.values():
            if not (job.finished_at or job.error) and any(
                t.replaces is not None and t.track.key == key for t in job.tasks
            ):
                raise ValueError("This song is already being replaced. See Downloads.")
        # the video saved until now stays on offer, so the swap can be undone
        previous = Alternative(candidate=saved.candidate, score=saved.score, reason=saved.reason)
        match = Match(
            track=record.track,
            candidate=picked.candidate.model_copy(update={"source": "manual"}),
            score=100.0,
            reason="you chose this video",
            alternatives=[previous, *(a for a in options if a is not picked)],
        )
        fmt = record_format(record)
        job = Job(
            input=record.track.display,
            format=fmt,
            collection=Collection(
                kind="track",
                name=f"{record.track.title} (another match)",
                tracks=[record.track],
                source="review",
            ),
            tasks=[
                TrackTask(track=record.track, position=1, match=match, replaces=str(record.path))
            ],
        )
        self.jobs[job.id] = job
        self.loop = asyncio.get_running_loop()
        self._publish("job", job_summary(job))
        pipeline = self.services.pipeline(on_event=self._on_event, settings=self._settings_for(fmt))
        self._spawn(self._run(job, pipeline))
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
        tasks = [
            TrackTask(
                track=t.track,
                position=t.position,
                # a replacement keeps the video the user chose and the file it replaces
                replaces=t.replaces,
                match=t.match if t.replaces is not None else None,
            )
            for t in failed
        ]
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
