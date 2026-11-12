"""Video (and audio-from-video) downloads from YouTube and the other sites yt-dlp supports.

Music goes through the Spotify -> match -> download pipeline; this module handles a link
to the video itself. It reuses the yt-dlp workarounds from :mod:`tunegrab.download.ytdlp`
(JS runtimes, player-client ladder, error kinds) and adds:

* **Quality**: the best stream up to the chosen height, H.264 + AAC first so the MP4
  plays everywhere (phones, TVs, Windows' Photos app); above 1080p YouTube only offers
  VP9/AV1, which are used then. AI-upscaled "super resolution" (``-sr``, yt-dlp#15433)
  and "stable volume" (``-drc``, yt-dlp#8259) formats are skipped.
* **Clips**: a start/end range is cut by TuneGrab's own ffmpeg step after the download,
  frame-accurate, instead of yt-dlp's ``--download-sections`` (broken with some ffmpeg
  builds: yt-dlp#16546).
* **Files**: ``<video folder>/<channel>/<title> [<id>].mp4`` (audio only:
  ``<music folder>/<channel>/<title> [<id>].<format>``); one that's already there is skipped.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from tunegrab.config import AudioFormat, Settings, VideoQuality
from tunegrab.download.convert import NO_DRC, format_selector, postprocessors
from tunegrab.download.ytdlp import (
    CLIENT_LADDER,
    AttemptDirs,
    DownloadCancelled,
    DownloadError,
    SourceInfo,
    _YtDlpLogger,
    base_options,
    classify_error,
    clean_error,
    extract,
    run_ladder,
    source_info,
)
from tunegrab.library.paths import sanitize_segment, unique_path
from tunegrab.models import Collection, Job, TaskStatus, Track, TrackTask

log = logging.getLogger(__name__)

VideoMode = Literal["video", "audio"]
VIDEO_QUALITIES: tuple[VideoQuality, ...] = ("best", "2160", "1440", "1080", "720", "480", "360")
NO_SR = "[format_id!$=-sr]"  # YouTube's AI-upscaled "super resolution" copies
MAX_PLAYLIST_VIDEOS = 2000
TMP_DIR_NAME = ".tunegrab-tmp"
FFMPEG_TIMEOUT_S = 3600

EventCallback = Callable[[Job, TrackTask | None], None]


class VideoError(ValueError):
    """The link can't be used (unsupported site, private, not a video, ...)."""


# --- formats -----------------------------------------------------------------------------


def video_format(quality: VideoQuality) -> str:
    """yt-dlp format selector: best video up to ``quality`` + best audio, merged."""
    height = "" if quality == "best" else f"[height<={quality}]"
    return (
        f"bv*{height}{NO_SR}+ba{NO_DRC}/"
        f"b{height}{NO_SR}/"
        f"bv*{height}+ba/b{height}/"  # super resolution / DRC if that's all there is
        "bv*+ba/b"
    )


def video_sort(quality: VideoQuality) -> list[str]:
    """Prefer the highest resolution up to ``quality``, then H.264 + AAC (plays anywhere)."""
    return ["res" if quality == "best" else f"res:{quality}", "vcodec:h264", "acodec:aac"]


# --- clip times --------------------------------------------------------------------------

_TIME = re.compile(r"^(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d+)?)$")


def parse_time(text: str | None) -> float | None:
    """``"83"``, ``"1:23"``, ``"1:02:03"``, ``"83.5"`` -> seconds; blank -> None."""
    if text is None or not text.strip():
        return None
    found = _TIME.match(text.strip())
    if found is None:
        raise VideoError(f"Not a time: {text!r} (use seconds, m:ss or h:mm:ss)")
    first, second, seconds = found.groups()
    hours, minutes = (first, second) if second is not None else (None, first)
    total = int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds)
    if minutes is not None and float(seconds) >= 60:
        raise VideoError(f"Not a time: {text!r} (seconds must be under 60)")
    return total


def format_time(seconds: float) -> str:
    whole = int(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    frac = seconds - whole
    text = f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
    return text + (f".{round(frac * 10)}" if frac >= 0.05 else "")


def clip_stamp(seconds: float) -> str:
    """``43`` -> ``0m43s``; ``3723.5`` -> ``1h02m03.5s``."""
    text = format_time(seconds)
    parts = text.split(":")
    units = ["h", "m", "s"][-len(parts) :]
    return "".join(f"{part}{unit}" for part, unit in zip(parts, units, strict=True))


def check_clip(
    start: float | None, end: float | None, duration: float | None
) -> tuple[float | None, float | None]:
    if start is not None and start < 0:
        raise VideoError("The clip can't start before 0:00.")
    if start is not None and end is not None and end <= start:
        raise VideoError("The clip must end after it starts.")
    if duration and start is not None and start >= duration:
        raise VideoError(f"The video is only {format_time(duration)} long.")
    if duration and end is not None and end >= duration:
        end = None  # "to the end"
    if start == 0:
        start = None
    return start, end


# --- probing -----------------------------------------------------------------------------


@dataclass(frozen=True)
class VideoEntry:
    id: str
    url: str
    title: str
    uploader: str
    duration_s: float | None = None
    thumbnail: str | None = None


@dataclass(frozen=True)
class VideoProbe:
    url: str
    kind: Literal["video", "playlist"]
    title: str
    uploader: str
    site: str
    entries: list[VideoEntry]
    thumbnail: str | None = None
    heights: list[int] = field(default_factory=list)  # available video heights, if known

    def payload(self) -> dict[str, Any]:
        first = self.entries[0] if self.entries else None
        return {
            "url": self.url,
            "kind": self.kind,
            "title": self.title,
            "uploader": self.uploader,
            "site": self.site,
            "count": len(self.entries),
            "duration_s": first.duration_s if self.kind == "video" and first else None,
            "thumbnail": self.thumbnail,
            "heights": self.heights,
            "entries": [
                {"id": e.id, "title": e.title, "uploader": e.uploader, "duration_s": e.duration_s}
                for e in self.entries[:50]
            ],
        }


def _thumbnail(info: dict[str, Any]) -> str | None:
    if info.get("thumbnail"):
        return str(info["thumbnail"])
    thumbs = [t for t in info.get("thumbnails") or [] if t.get("url")]
    return str(thumbs[-1]["url"]) if thumbs else None


def _uploader(info: dict[str, Any], fallback: str = "") -> str:
    for key in ("uploader", "channel", "creator", "artist", "playlist_uploader"):
        if info.get(key):
            return str(info[key])
    return fallback or "Unknown"


def _entry(info: dict[str, Any], fallback_uploader: str = "") -> VideoEntry | None:
    video_id = str(info.get("id") or "")
    url = info.get("webpage_url") or info.get("original_url") or info.get("url")
    if not video_id or not url:
        return None
    duration = info.get("duration")
    return VideoEntry(
        id=video_id,
        url=str(url),
        title=str(info.get("title") or video_id),
        uploader=_uploader(info, fallback_uploader),
        duration_s=float(duration) if duration else None,
        thumbnail=_thumbnail(info),
    )


def _is_nested_list(raw: dict[str, Any]) -> bool:
    return raw.get("_type") == "playlist" or raw.get("ie_key") == "YoutubeTab"


_VCODECS = {"avc1": "H.264", "vp09": "VP9", "vp9": "VP9", "av01": "AV1", "hev1": "HEVC"}


def video_label(info: Any) -> str | None:
    """``"1080p60 H.264"`` for the video stream yt-dlp picked, if any."""
    if not isinstance(info, dict):
        return None
    parts = info.get("requested_formats") or [info]
    video = next((f for f in parts if f.get("vcodec") not in (None, "none")), None)
    if video is None or not video.get("height"):
        return None
    codec = str(video.get("vcodec") or "").split(".")[0]
    fps = video.get("fps") or 0
    rate = str(round(fps)) if fps > 30 else ""
    return f"{video['height']}p{rate} {_VCODECS.get(codec, codec.upper())}".strip()


def probe_info(url: str, info: dict[str, Any]) -> VideoProbe:
    """Turn yt-dlp's ``extract_info(download=False)`` result into a :class:`VideoProbe`."""
    site = str(info.get("extractor_key") or info.get("extractor") or "")
    if info.get("_type") in ("playlist", "multi_video") or "entries" in info:
        uploader = _uploader(info)
        raws = [r for r in info.get("entries") or [] if isinstance(r, dict)]
        tabs = [r for r in raws if _is_nested_list(r)]
        entries = [
            entry
            for raw in raws[:MAX_PLAYLIST_VIDEOS]
            if not _is_nested_list(raw) and (entry := _entry(raw, uploader)) is not None
        ]
        if not entries and tabs:
            raise VideoError(
                "That's a channel page. Open its Videos (or Shorts) tab and paste that link, "
                "e.g. https://www.youtube.com/@name/videos"
            )
        if not entries:
            raise VideoError("That playlist has no videos TuneGrab can download.")
        return VideoProbe(
            url=url,
            kind="playlist",
            title=str(info.get("title") or "Playlist"),
            uploader=uploader,
            site=site,
            entries=entries,
            thumbnail=_thumbnail(info) or entries[0].thumbnail,
        )
    entry = _entry(info)
    if entry is None:
        raise VideoError("yt-dlp found nothing to download at that link.")
    heights = sorted(
        {
            int(f["height"])
            for f in info.get("formats") or []
            if f.get("height")
            and f.get("vcodec") not in (None, "none")
            and not str(f.get("format_id", "")).endswith("-sr")
        },
        reverse=True,
    )
    return VideoProbe(
        url=url,
        kind="video",
        title=entry.title,
        uploader=entry.uploader,
        site=site,
        entries=[entry],
        thumbnail=entry.thumbnail,
        heights=heights,
    )


def probe(url: str, cookies_from_browser: str | None = None) -> VideoProbe:
    """What's at ``url``: one video, or a playlist / channel's videos (titles only)."""
    url = url.strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        if "." not in url.split("/")[0]:
            raise VideoError("Paste a full link, e.g. https://www.youtube.com/watch?v=...")
        url = "https://" + url
    import yt_dlp

    logger = _YtDlpLogger()
    options = base_options(logger, None, None, cookies_from_browser, None)
    options.update(
        {
            "extract_flat": "in_playlist",
            "playlistend": MAX_PLAYLIST_VIDEOS,
            "skip_download": True,
            "noplaylist": True,  # watch?v=X&list=Y means the video, as on YouTube
        }
    )
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        message = clean_error(str(exc)) or logger.last_error or "yt-dlp failed"
        if "unsupported url" in message.lower():
            raise VideoError("yt-dlp doesn't support that site or link.") from exc
        raise DownloadError(message, classify_error(message)) from exc
    if not isinstance(info, dict):
        raise VideoError("yt-dlp found nothing to download at that link.")
    return probe_info(url, info)


# --- downloading -------------------------------------------------------------------------


@dataclass(frozen=True)
class VideoOptions:
    mode: VideoMode = "video"
    quality: VideoQuality = "1080"
    start: float | None = None
    end: float | None = None

    @property
    def is_clip(self) -> bool:
        return self.start is not None or self.end is not None

    def clip_suffix(self) -> str:
        """`` (clip 0m43s-0m52.5s)`` for file names (no colons on Windows)."""
        if not self.is_clip:
            return ""
        end = clip_stamp(self.end) if self.end is not None else "end"
        return f" (clip {clip_stamp(self.start or 0)}-{end})"


class VideoDownloader:
    """Downloads one video (or its audio) into a folder; no naming, no moving."""

    def __init__(
        self,
        options: VideoOptions,
        audio_format: AudioFormat = "m4a",
        bitrate: int = 320,
        cookies_from_browser: str | None = None,
        ladder: tuple[tuple[str, ...] | None, ...] = CLIENT_LADDER,
    ) -> None:
        self.options = options
        self.audio_format = audio_format
        self.bitrate = bitrate
        self.cookies_from_browser = cookies_from_browser
        self.ladder = ladder

    @property
    def extension(self) -> str:
        return "mp4" if self.options.mode == "video" else self.audio_format

    def ydl_options(
        self,
        dest_dir: Path,
        progress: Callable[[float, str], None] | None,
        cancel: threading.Event | None,
        logger: _YtDlpLogger,
        client: tuple[str, ...] | None,
    ) -> dict[str, Any]:
        options = base_options(logger, cancel, progress, self.cookies_from_browser, client)
        options.update(
            {
                "outtmpl": "%(id)s.%(ext)s",  # relative to paths.home
                "paths": {"home": str(dest_dir), "temp": str(dest_dir)},
                "windowsfilenames": True,
            }
        )
        if self.options.mode == "video":
            options.update(
                {
                    "format": video_format(self.options.quality),
                    "format_sort": video_sort(self.options.quality),
                    "merge_output_format": "mp4",
                    "postprocessors": [{"key": "FFmpegMetadata", "add_metadata": True}],
                }
            )
        else:
            options.update(
                {
                    "format": format_selector(self.audio_format),
                    "postprocessors": [
                        *postprocessors(self.audio_format, self.bitrate),
                        {"key": "FFmpegMetadata", "add_metadata": True},
                    ],
                }
            )
        return options

    def download(
        self,
        url: str,
        dest_dir: Path,
        progress: Callable[[float, str], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> tuple[Path, str | None, bool]:
        """Returns (file, what was downloaded e.g. "1080p H.264 + AAC 129 kbps", low quality)."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        labels: dict[Path, str | None] = {}
        dirs = AttemptDirs(dest_dir)

        def attempt(client: tuple[str, ...] | None) -> tuple[Path, SourceInfo]:
            work = dirs.next()
            logger = _YtDlpLogger()
            info = extract(
                url, self.ydl_options(work, progress, cancel, logger, client), logger, cancel
            )
            path = self._find_output(info, work)
            audio = source_info(info, client, logger.warnings)
            parts = [video_label(info) if self.options.mode == "video" else None]
            if audio.codec:  # "low quality" only when YouTube held better formats back
                parts.append(audio.label if audio.worth_retrying else audio.plain_label)
            labels[path] = " + ".join(p for p in parts if p) or None
            return path, audio

        # The low-quality guard only matters when asking for more than 360p.
        ladder = self.ladder if self.options.quality != "360" else self.ladder[:1]
        path, audio = run_ladder(attempt, cancel, ladder)
        return dirs.settle(path), labels.get(path), audio.worth_retrying

    def _find_output(self, info: Any, dest_dir: Path) -> Path:
        wanted = "." + self.extension
        if isinstance(info, dict):
            for item in info.get("requested_downloads") or []:
                path = Path(item.get("filepath") or "")
                if path.suffix == wanted and path.exists():
                    return path
        found = sorted(p for p in dest_dir.iterdir() if p.suffix == wanted)
        if found:
            return found[0]
        raise DownloadError(f"yt-dlp finished but no {wanted} file was produced")


def clip_command(source: Path, target: Path, options: VideoOptions) -> list[str]:
    """ffmpeg command cutting ``source`` to the clip range.

    Video is re-encoded (H.264, visually lossless CRF 18) so the cut is frame-accurate;
    audio-only clips are stream-copied (audio frames are ~20 ms, so that's accurate)."""
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if options.start is not None:
        command += ["-ss", f"{options.start:.3f}"]
    command += ["-i", str(source)]
    if options.end is not None:
        command += ["-t", f"{options.end - (options.start or 0):.3f}"]
    command += ["-map", "0", "-map_metadata", "0"]
    if options.mode == "video":
        command += [
            *("-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p"),
            *("-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"),
        ]
    else:
        command += ["-c", "copy"]
    return [*command, str(target)]


def cut_clip(path: Path, options: VideoOptions, cancel: threading.Event | None) -> Path:
    target = path.with_name(f"{path.stem}.clip{path.suffix}")
    process = subprocess.Popen(
        clip_command(path, target, options),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    deadline = time.monotonic() + FFMPEG_TIMEOUT_S
    while process.poll() is None:
        if (cancel is not None and cancel.is_set()) or time.monotonic() > deadline:
            process.kill()
            process.wait()
            target.unlink(missing_ok=True)
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled()
            raise DownloadError("ffmpeg took too long to cut the clip")
        time.sleep(0.2)
    stderr = process.stderr.read() if process.stderr else ""
    if process.returncode != 0 or not target.exists():
        target.unlink(missing_ok=True)
        raise DownloadError(f"ffmpeg couldn't cut the clip: {stderr.strip()[-300:]}")
    path.unlink(missing_ok=True)
    return target


def final_path(root: Path, entry: VideoEntry, options: VideoOptions, ext: str) -> Path:
    folder = sanitize_segment(entry.uploader)
    name = sanitize_segment(f"{entry.title}{options.clip_suffix()} [{entry.id}]")
    return root / folder / f"{name}.{ext}"


# --- jobs --------------------------------------------------------------------------------


def build_job(found: VideoProbe, options: VideoOptions) -> tuple[Job, dict[str, VideoEntry]]:
    """A Job (for the Downloads list) with one task per video; returns task id -> entry."""
    collection = Collection(
        kind="video",
        id=found.url,
        name=found.title + options.clip_suffix(),
        owner=found.uploader,
        source=f"video:{options.mode}",
    )
    tasks: list[TrackTask] = []
    entries: dict[str, VideoEntry] = {}
    for position, entry in enumerate(found.entries, start=1):
        track = Track(
            title=entry.title,
            artists=[entry.uploader],
            duration_ms=int((entry.duration_s or 0) * 1000),
            cover_url=entry.thumbnail,
        )
        task = TrackTask(track=track, position=position)
        tasks.append(task)
        entries[task.id] = entry
    collection.tracks = [t.track for t in tasks]
    return Job(input=found.url, collection=collection, tasks=tasks), entries


@dataclass
class VideoPipeline:
    """Runs a video job: one video at a time (sites throttle parallel video downloads)."""

    settings: Settings
    options: VideoOptions
    entries: dict[str, VideoEntry]
    downloader: VideoDownloader | None = None
    on_event: EventCallback | None = None
    retry_backoff: float = 2.0
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        if self.downloader is None:
            self.downloader = VideoDownloader(
                self.options,
                self.settings.format,
                self.settings.bitrate,
                self.settings.cookies_from_browser,
            )

    @property
    def root(self) -> Path:
        """Videos go to the video folder; audio-only downloads into the music library."""
        if self.options.mode == "audio":
            return self.settings.resolved_output_dir
        return self.settings.resolved_video_dir

    def cancel(self) -> None:
        self.cancel_event.set()

    def _emit(self, job: Job, task: TrackTask | None = None) -> None:
        if self.on_event is not None:
            try:
                self.on_event(job, task)
            except Exception:
                log.exception("event callback failed")

    def _set(self, job: Job, task: TrackTask, status: TaskStatus, message: str = "") -> None:
        task.status = status
        task.message = message
        self._emit(job, task)

    async def run(self, job: Job) -> Job:
        self._emit(job)
        for task in job.tasks:
            if self.cancel_event.is_set() or job.cancelled:
                self._set(job, task, TaskStatus.SKIPPED, "cancelled")
                continue
            await asyncio.to_thread(self._process, job, task)
        with contextlib.suppress(OSError):
            (self.root / TMP_DIR_NAME).rmdir()
        job.cancelled = job.cancelled or self.cancel_event.is_set()
        job.finished_at = datetime.now(UTC)
        self._emit(job)
        return job

    def _process(self, job: Job, task: TrackTask) -> None:
        assert self.downloader is not None
        entry = self.entries[task.id]
        target = final_path(self.root, entry, self.options, self.downloader.extension)
        if target.exists():
            task.path = str(target)
            self._set(job, task, TaskStatus.SKIPPED, "already downloaded")
            return
        attempts = self.settings.retries + 1
        for attempt in range(1, attempts + 1):
            task.attempts = attempt
            try:
                self._download(job, task, entry, target)
                return
            except DownloadCancelled:
                self._set(job, task, TaskStatus.SKIPPED, "cancelled")
                return
            except (DownloadError, OSError) as exc:
                kind = exc.kind if isinstance(exc, DownloadError) else "other"
                if attempt < attempts and kind != "unavailable":
                    log.info("retrying video %s after: %s", entry.url, exc)
                    if self.cancel_event.wait(self.retry_backoff * attempt):
                        self._set(job, task, TaskStatus.SKIPPED, "cancelled")
                        return
                    continue
                task.error = str(exc)
                task.failed_stage = "downloading"
                log.warning("video failed: %s: %s", entry.url, exc)
                self._set(job, task, TaskStatus.FAILED, task.error)
                return
            except Exception as exc:  # one broken video must not stop the rest of the job
                log.exception("video failed unexpectedly: %s", entry.url)
                task.error = f"Something unexpected went wrong ({type(exc).__name__}: {exc})"
                task.failed_stage = "downloading"
                self._set(job, task, TaskStatus.FAILED, task.error)
                return

    def _download(self, job: Job, task: TrackTask, entry: VideoEntry, target: Path) -> None:
        assert self.downloader is not None
        last = [0.0]

        def progress(fraction: float, phase: str) -> None:
            task.progress = fraction
            if phase == "converting" and task.status != TaskStatus.CONVERTING:
                self._set(job, task, TaskStatus.CONVERTING)
            elif time.monotonic() - last[0] > 0.25:
                last[0] = time.monotonic()
                self._emit(job, task)

        self._set(job, task, TaskStatus.DOWNLOADING)
        tmp_root = self.root / TMP_DIR_NAME
        tmp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root, prefix="video-") as tmp:
            path, label, low_quality = self.downloader.download(
                entry.url, Path(tmp), progress=progress, cancel=self.cancel_event
            )
            if self.options.is_clip:
                self._set(job, task, TaskStatus.CONVERTING, "cutting the clip")
                path = cut_clip(path, self.options, self.cancel_event)
            target.parent.mkdir(parents=True, exist_ok=True)
            final = unique_path(target)
            shutil.move(str(path), final)
        task.source = label
        task.low_quality = low_quality
        task.path = str(final)
        task.progress = 1.0
        self._set(job, task, TaskStatus.DONE)
