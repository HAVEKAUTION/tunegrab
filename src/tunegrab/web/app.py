"""FastAPI app: REST endpoints + Server-Sent Events, reusing pipeline.py.

Binds to 127.0.0.1 by default (see ``tunegrab serve``). There is no authentication,
so it must not be exposed to untrusted networks.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from tunegrab import __version__, storage
from tunegrab.browse import SearchResults
from tunegrab.config import (
    COOKIE_BROWSERS,
    Settings,
    coerce_setting,
    config_file,
    data_dir,
    log_dir,
    pin_music_folder,
    read_config_file,
    startup_warnings,
    write_config_file,
)
from tunegrab.doctor import Status, check_output_dir, run_all, update_ytdlp
from tunegrab.download.ytdlp import DownloadError
from tunegrab.errors import TuneGrabError
from tunegrab.library.paths import TemplateError, validate_template
from tunegrab.matching.base import SearchError
from tunegrab.metadata.base import MetadataError, NotFoundError
from tunegrab.models import Candidate, Job, TaskStatus, Track
from tunegrab.net import NetworkError
from tunegrab.runner import unexpected_message as unexpected_error
from tunegrab.services import Services, build_services
from tunegrab.updates import UpdateChecker
from tunegrab.urls import (
    InvalidURLError,
    SpotifyRef,
    is_spotify_input,
    is_video_link,
    parse_youtube_id,
)
from tunegrab.video import (
    VideoError,
    check_clip,
    parse_time,
)
from tunegrab.web.desktop import open_in_file_manager, pick_folder
from tunegrab.web.jobs import (
    CleanRequest,
    JobManager,
    JobRequest,
    VideoRequest,
    job_summary,
    release_payload,
    task_payload,
)

log = logging.getLogger(__name__)

SYSTEM_CACHE_S = 60.0
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})
HEARTBEAT_S = 15.0
SHUTDOWN_WAIT_S = 15.0  # how long a stopping server lets running songs finish cleanly

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
    "cookies_from_browser",
    "check_updates",
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
        if key == "cookies_from_browser" and value:
            value = str(value).lower()
            if value not in COOKIE_BROWSERS:
                raise ValueError(
                    f"cookies_from_browser must be one of {', '.join(COOKIE_BROWSERS)}"
                )
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
    services = services or build_services(settings or pin_music_folder(Settings()))
    manager = JobManager(services)
    updates = UpdateChecker(services.http, data_dir() / "update-check.json")

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
        if services.settings.check_updates:
            updates.check_in_background()  # at most once a day; shows on a later refresh
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
            "failures": len(services.archive.open_failures())
            + len(services.archive.video_failures()),
            "warnings": list(startup_warnings),
            "updates": updates.info().payload() if services.settings.check_updates else None,
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

    def static_file(name: str, media_type: str) -> Response:
        body = files("tunegrab.web").joinpath(f"static/{name}").read_text(encoding="utf-8")
        # no-cache: the browser revalidates, so an updated TuneGrab never runs old scripts
        return Response(body, media_type=media_type, headers={"Cache-Control": "no-cache"})

    @app.get("/", response_class=HTMLResponse)
    def index() -> Response:
        return static_file("index.html", "text/html; charset=utf-8")

    @app.get("/app.js")
    def app_js() -> Response:
        return static_file("app.js", "text/javascript; charset=utf-8")

    @app.get("/app.css")
    def app_css() -> Response:
        return static_file("app.css", "text/css; charset=utf-8")

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "metadata": services.resolver.provider_names}

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
    async def create_job(request: JobRequest) -> Any:
        sources = [
            bool(request.url and request.url.strip()),
            bool(request.ytm_album),
            bool(request.spotify_tracks),
            bool(request.csv_text and request.csv_text.strip()),
            bool(request.ytm_songs),
        ]
        if sum(sources) != 1:
            raise HTTPException(
                400, "Give one of url, ytm_album, ytm_songs, spotify_tracks or csv_text"
            )
        if request.url and is_video_link(request.url) and not request.track_ids:
            # "Download now" on a YouTube (etc.) link: the video's audio, as music
            video = VideoRequest(url=request.url, mode="audio", format=request.format)
            return job_summary(await manager.submit_video(video))
        if request.url and not request.track_ids and not is_spotify_input(request.url):
            return await download_search(request.url.strip(), request)
        job = await manager.submit(request)
        return job_summary(job)

    async def download_search(text: str, request: JobRequest) -> Any:
        """Download now on typed text: the top song, named up front. An artist's name
        isn't a song, so the page is sent to Browse instead (409 with ``browse``)."""
        try:
            artist = await asyncio.to_thread(services.browser.ytmusic.named_artist, text)
        except SearchError as exc:  # still worth trying the song search
            log.warning("artist check failed for %r: %s", text, exc)
            artist = None
        if artist is not None:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": f"\u201c{artist.name}\u201d is an artist. Pick albums or songs.",
                    "browse": text,
                },
            )
        try:
            built = await asyncio.to_thread(services.pipeline().build_job, text)
        except LookupError as exc:
            raise HTTPException(
                404, f"Nothing on YouTube Music matches \u201c{text}\u201d."
            ) from exc
        return job_summary(await manager.submit(request, built))

    def _browse_error(exc: Exception) -> HTTPException:
        if isinstance(exc, InvalidURLError):
            return HTTPException(400, str(exc))
        if isinstance(exc, NotFoundError):
            return HTTPException(404, str(exc))
        return HTTPException(502, str(exc))

    @app.get("/api/browse")
    async def browse(url: str) -> dict[str, Any]:
        """Artist -> its releases; album/playlist/track -> its tracks; plain text -> the
        artist it names, or matching albums and songs."""
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
                hits = await asyncio.to_thread(services.browser.ytmusic.lookup, text)
                if isinstance(hits, SearchResults):
                    return {"type": "search", **hits.model_dump(mode="json")}
                return {"type": "artist", **hits.model_dump(mode="json")}
            ref = await asyncio.to_thread(services.resolver.parse, text)
            if ref.kind == "artist":
                overview = await asyncio.to_thread(services.browser.spotify_artist, ref.id)
                return {"type": "artist", **overview.model_dump(mode="json")}
            collection = await asyncio.to_thread(services.resolver.resolve_ref, ref)
        except (InvalidURLError, MetadataError, SearchError) as exc:
            raise _browse_error(exc) from exc
        return {"type": "collection", **release_payload(collection)}

    @app.get("/api/browse/ytmusic")
    async def browse_ytmusic(artist: str, channel: str | None = None) -> dict[str, Any]:
        try:
            overview = await asyncio.to_thread(services.browser.ytmusic.artist, artist, channel)
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
        if job_id in manager.video_jobs:
            raise HTTPException(409, "Videos are downloaded from their own link; press Retry.")
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
            "videos": [
                {
                    "key": v.key,
                    "url": v.url,
                    "title": v.entry.get("title") or v.url,
                    "uploader": v.entry.get("uploader") or "",
                    "duration_s": v.entry.get("duration_s"),
                    "mode": v.options.get("mode"),
                    "clip": v.options.get("start") is not None or v.options.get("end") is not None,
                    "format": v.format,
                    "source": v.source,
                    "path": str(v.path) if v.path else None,
                    "exists": v.exists,
                    "downloaded_at": v.updated_at,
                }
                for v in services.archive.all_videos()
            ],
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
        """Show the music folder, the video folder, or a file TuneGrab saved, in the file
        manager. Anything else is refused: the page must not open arbitrary paths."""
        root = services.settings.resolved_output_dir.resolve()
        roots = (root, services.settings.resolved_video_dir.resolve())
        target = Path(body.path).resolve() if body.path else root
        inside = any(target == r or r in target.parents for r in roots)
        recorded = body.path is not None and (
            services.archive.path_is_recorded(Path(body.path))
            or services.archive.path_is_recorded(target)
        )
        if not (inside or recorded):
            raise HTTPException(400, "TuneGrab only opens your music and video folders.")
        if not target.exists():
            if target not in roots:
                raise HTTPException(404, "That file isn't there any more (moved or deleted?).")
            target.mkdir(parents=True, exist_ok=True)
        try:
            open_in_file_manager(target)
        except OSError as exc:
            raise HTTPException(500, f"Couldn't open the file manager: {exc}") from exc
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
