"""yt-dlp used as a library: format selection, progress, cancellation and the
workarounds for YouTube's moving targets (see the yt-dlp issue tracker):

* **JS runtimes** - every supported runtime is enabled, Deno first (yt-dlp's default is
  Deno only, and Deno's development ends in 2027: yt-dlp#17799).
* **Player clients** - when YouTube answers 403 / "unplayable" / only format 18, the same
  video is retried as other YouTube players (the workarounds from yt-dlp#17989, #17389).
* **Quality guard** - format 18 (360p video with ~96 kbps AAC) is what's left when the JS
  challenge fails (yt-dlp#17542, #17934); it is only accepted as a last resort and
  reported, never silently.
* **Error kinds** - "unavailable" (try another video), "rate_limited" (slow down) and
  "blocked" (try another player client) let the pipeline react instead of just retrying.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from tunegrab.config import AudioFormat
from tunegrab.download.convert import extension, format_selector, postprocessors
from tunegrab.errors import TuneGrabError

log = logging.getLogger(__name__)

# (fraction 0..1, phase: "downloading" | "converting")
ProgressCallback = Callable[[float, str], None]

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Every JS runtime yt-dlp supports, in order of preference (Deno is the most sandboxed).
JS_RUNTIMES = ("deno", "node", "bun", "quickjs")

# Player-client sets to try in turn; None = yt-dlp's own default choice.
CLIENT_LADDER: tuple[tuple[str, ...] | None, ...] = (
    None,
    ("default", "-web_embedded"),
    ("web_safari",),
)
LOW_QUALITY_FORMATS = frozenset({"18", "17", "36"})  # muxed low-res video formats

ErrorKind = Literal["unavailable", "rate_limited", "blocked", "other"]

# yt-dlp warnings saying formats were dropped (JS challenge / PO token / SABR trouble):
# a low-quality result *with* one of these is worth retrying with another client; without
# one, the video really has nothing better (e.g. a 2005 upload).
_DEGRADED = re.compile(
    r"challenge|formats may be missing|formats have been skipped|po token|sabr", re.IGNORECASE
)

_UNAVAILABLE = re.compile(
    r"video unavailable|not available in your country|not made this video available|"
    r"has been removed|no longer available|private video|account .* (?:terminated|closed)|"
    r"copyright (?:claim|grounds)|members[- ]only|join this channel|premieres in|"
    r"live event will begin|sign in to confirm your age|age[- ]restricted|"
    r"inappropriate for some users|has been blocked",
    re.IGNORECASE,
)
_RATE_LIMITED = re.compile(
    r"http error 429|too many requests|rate[- ]?limit|try again later|"
    r"confirm you(?:'|\u2019)?re not a bot",
    re.IGNORECASE,
)
_BLOCKED = re.compile(
    r"http error 403|forbidden|page needs to be reloaded|unplayable|"
    r"requested format is not available|only images are available|"
    r"challenge solving failed|n(?:sig)? challenge|some formats may be missing|"
    r"downloaded file is empty|fragment request failed",
    re.IGNORECASE,
)


def classify_error(message: str) -> ErrorKind:
    if _RATE_LIMITED.search(message):
        return "rate_limited"
    if _UNAVAILABLE.search(message):
        return "unavailable"
    if _BLOCKED.search(message):
        return "blocked"
    return "other"


class DownloadError(TuneGrabError):
    """yt-dlp could not fetch or convert the audio."""

    def __init__(self, message: str, kind: ErrorKind | None = None) -> None:
        super().__init__(message)
        self.kind: ErrorKind = kind or classify_error(message)


# Shown when every player client was refused: almost always YouTube changed its player and
# yt-dlp needs a release to catch up (02b_YOUTUBE_CONSTRAINTS.md, measure 1).
BREAKAGE_HINT = "YouTube may have changed something: update yt-dlp in Settings, then press Retry."


AGE_HINT = (
    "This video is age-restricted, so YouTube only plays it when you're signed in. "
    "Choose the browser you use YouTube in under YouTube sign-in in Settings, then press Retry."
)
COOKIES_HINT = (
    "TuneGrab couldn't read the browser's YouTube sign-in. Close that browser and press "
    "Retry, or choose Firefox under YouTube sign-in in Settings."
)
_AGE = re.compile(
    r"sign in to confirm your age|age[- ]restricted|inappropriate for some users", re.IGNORECASE
)
_COOKIES = re.compile(
    r"cookies? database|decrypt with dpapi|failed to decrypt|could not find .*cookies|"
    r"failed to load cookies|unsupported browser specified for cookies",
    re.IGNORECASE,
)


def describe_failure(exc: BaseException) -> str:
    """The sentence shown for a failed download, with the fix when there is one."""
    message = str(exc) or type(exc).__name__
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, DownloadError):
            if current.kind == "blocked":
                return f"YouTube refused this download ({current}). {BREAKAGE_HINT}"
            if _COOKIES.search(str(current)):
                return f"{COOKIES_HINT} ({current})"
            if _AGE.search(str(current)):
                return AGE_HINT
        current = current.__cause__
    return message


class DownloadCancelled(DownloadError):
    """The user cancelled (Ctrl+C) while downloading."""

    def __init__(self, message: str = "cancelled") -> None:
        super().__init__(message, "other")


@dataclass(frozen=True)
class SourceInfo:
    """What YouTube actually served (before any conversion)."""

    format_id: str | None
    codec: str | None
    bitrate_kbps: float | None
    client: str | None = None  # the player-client workaround used, if any
    degraded: bool = True  # yt-dlp warned that formats were missing (see _DEGRADED)

    @property
    def low_quality(self) -> bool:
        return self.format_id in LOW_QUALITY_FORMATS or bool(
            self.bitrate_kbps and self.bitrate_kbps < 64
        )

    @property
    def worth_retrying(self) -> bool:
        """Low quality because YouTube held formats back, not because that's all there is."""
        return self.low_quality and self.degraded

    @property
    def plain_label(self) -> str:
        """``"Opus 129 kbps"``, without the low-quality note."""
        codec = {"opus": "Opus", "mp4a": "AAC", "vorbis": "Vorbis"}.get(
            (self.codec or "").split(".")[0], (self.codec or "audio").split(".")[0]
        )
        rate = f" {self.bitrate_kbps:.0f} kbps" if self.bitrate_kbps else ""
        return f"{codec}{rate}"

    @property
    def label(self) -> str:
        return self.plain_label + (" (low quality source)" if self.low_quality else "")


SourceCallback = Callable[[SourceInfo], None]


def source_info(
    info: Any, client: tuple[str, ...] | None = None, warnings: list[str] | None = None
) -> SourceInfo:
    """The format yt-dlp picked; for merged/converted results the original stream."""
    if not isinstance(info, dict):
        return SourceInfo(None, None, None)
    chosen = (info.get("requested_formats") or [info])[-1]  # audio part when merged
    acodec = chosen.get("acodec") or info.get("acodec")
    bitrate = chosen.get("abr") or info.get("abr")
    if not bitrate and chosen.get("vcodec") in (None, "none"):
        bitrate = chosen.get("tbr")  # audio-only: total bitrate is the audio bitrate
    return SourceInfo(
        format_id=str(chosen.get("format_id") or info.get("format_id") or "") or None,
        codec=None if acodec in (None, "none") else str(acodec),
        bitrate_kbps=float(bitrate) if bitrate else None,
        client=",".join(client) if client else None,
        degraded=warnings is None or any(_DEGRADED.search(w) for w in warnings),
    )


def available_js_runtimes() -> dict[str, dict[str, Any]]:
    """yt-dlp ``js_runtimes`` param: all supported runtimes, Deno first. yt-dlp checks
    which are installed and skips the rest."""
    return {name: {} for name in JS_RUNTIMES}


class Downloader(Protocol):
    def download(
        self,
        video_id: str,
        dest_dir: Path,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
        source: SourceCallback | None = None,
    ) -> Path: ...


class _YtDlpLogger:
    def __init__(self) -> None:
        self.last_error = ""
        self.warnings: list[str] = []

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        text = _ANSI.sub("", msg)
        self.warnings.append(text)
        log.info("yt-dlp: %s", text)

    def error(self, msg: str) -> None:
        self.last_error = _ANSI.sub("", msg)
        log.debug("yt-dlp error: %s", self.last_error)


def clean_error(message: str) -> str:
    message = _ANSI.sub("", message).strip()
    return message.removeprefix("ERROR:").strip()


def base_options(
    logger: _YtDlpLogger,
    cancel: threading.Event | None,
    progress: ProgressCallback | None,
    cookies_from_browser: str | None,
    client: tuple[str, ...] | None,
) -> dict[str, Any]:
    """yt-dlp options shared by audio and video downloads."""

    def progress_hook(status: dict[str, Any]) -> None:
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled()
        if progress is None:
            return
        if status.get("status") == "downloading":
            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            done = status.get("downloaded_bytes") or 0
            if total:
                progress(min(1.0, done / total), "downloading")
        elif status.get("status") == "finished":
            progress(1.0, "downloading")

    def postprocessor_hook(status: dict[str, Any]) -> None:
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled()
        if progress is not None and status.get("status") == "started":
            progress(1.0, "converting")

    options: dict[str, Any] = {
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        "logger": logger,
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "noplaylist": True,
        "overwrites": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        # Measured: the disk cache saves nothing per song with the EJS solver, so keep
        # TuneGrab from leaving a cache folder behind.
        "cachedir": False,
        "js_runtimes": available_js_runtimes(),
    }
    if client:
        options["extractor_args"] = {"youtube": {"player_client": list(client)}}
    if cookies_from_browser:
        options["cookiesfrombrowser"] = (cookies_from_browser,)
    return options


class AttemptDirs:
    """A separate folder per player-client attempt, so a kept low-quality result from an
    earlier attempt is never overwritten or deleted by a later one."""

    def __init__(self, dest_dir: Path) -> None:
        self.dest_dir = dest_dir
        self.count = 0

    def next(self) -> Path:
        self.count += 1
        if self.count == 1:
            return self.dest_dir
        work = self.dest_dir / f".client{self.count}"
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        return work

    def settle(self, path: Path) -> Path:
        """Move the chosen file into ``dest_dir`` and remove the other attempts."""
        final = path
        if path.parent != self.dest_dir:
            final = self.dest_dir / path.name
            final.unlink(missing_ok=True)
            shutil.move(str(path), final)
        for n in range(2, self.count + 1):
            shutil.rmtree(self.dest_dir / f".client{n}", ignore_errors=True)
        return final


def run_ladder(
    attempt: Callable[[tuple[str, ...] | None], tuple[Path, SourceInfo]],
    cancel: threading.Event | None,
    ladder: tuple[tuple[str, ...] | None, ...] = CLIENT_LADDER,
) -> tuple[Path, SourceInfo]:
    """Try ``attempt`` with each player-client set until one gives a good result.

    "blocked" errors and degraded low-quality results move on to the next client set;
    other errors (unavailable, rate-limited, ...) are raised straight away. A low-quality
    result is returned when no client did better (or a later client errored)."""
    fallback: tuple[Path, SourceInfo] | None = None
    last_error: DownloadError | None = None
    for index, client in enumerate(ladder):
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled()
        try:
            path, info = attempt(client)
        except DownloadCancelled:
            raise
        except DownloadError as exc:
            if fallback is not None:
                log.info("clients %s failed (%s); keeping the earlier result", client, exc)
                return fallback
            if exc.kind != "blocked":
                raise
            last_error = exc
            log.info("blocked with clients %s: %s", client or "default", exc)
            continue
        if not info.worth_retrying or index == len(ladder) - 1:
            return path, info
        log.info("only a low-quality source (%s) with clients %s", info.label, client)
        fallback = (path, info)
    if fallback is not None:
        return fallback
    assert last_error is not None
    raise last_error


class YtDlpDownloader:
    def __init__(
        self,
        fmt: AudioFormat = "mp3",
        bitrate: int = 320,
        cookies_from_browser: str | None = None,
        ladder: tuple[tuple[str, ...] | None, ...] = CLIENT_LADDER,
    ) -> None:
        self.fmt = fmt
        self.bitrate = bitrate
        self.cookies_from_browser = cookies_from_browser
        self.ladder = ladder

    def options(
        self,
        dest_dir: Path,
        progress: ProgressCallback | None,
        cancel: threading.Event | None,
        logger: _YtDlpLogger,
        client: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        options = base_options(logger, cancel, progress, self.cookies_from_browser, client)
        options.update(
            {
                "format": format_selector(self.fmt),
                "outtmpl": "%(id)s.%(ext)s",  # relative to paths.home
                "paths": {"home": str(dest_dir), "temp": str(dest_dir)},
                "postprocessors": postprocessors(self.fmt, self.bitrate),
            }
        )
        return options

    def download(
        self,
        video_id: str,
        dest_dir: Path,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
        source: SourceCallback | None = None,
    ) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        url = f"https://www.youtube.com/watch?v={video_id}"

        dirs = AttemptDirs(dest_dir)

        def attempt(client: tuple[str, ...] | None) -> tuple[Path, SourceInfo]:
            work = dirs.next()
            logger = _YtDlpLogger()
            options = self.options(work, progress, cancel, logger, client)
            info = extract(url, options, logger, cancel)
            path = self._find_output(info, work, video_id)
            return path, source_info(info, client, logger.warnings)

        path, info = run_ladder(attempt, cancel, self.ladder)
        if source is not None:
            source(info)
        return dirs.settle(path)

    def _find_output(self, info: Any, dest_dir: Path, video_id: str) -> Path:
        wanted = "." + extension(self.fmt)
        if isinstance(info, dict):
            for item in info.get("requested_downloads") or []:
                path = Path(item.get("filepath") or "")
                if path.suffix == wanted and path.exists():
                    return path
        matches = sorted(dest_dir.glob(f"{video_id}*{wanted}"))
        if matches:
            return matches[0]
        raise DownloadError(f"yt-dlp finished but no {wanted} file was produced for {video_id}")


def extract(
    url: str, options: dict[str, Any], logger: _YtDlpLogger, cancel: threading.Event | None
) -> Any:
    """Run yt-dlp; turn its exceptions into DownloadError / DownloadCancelled."""
    import yt_dlp

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadCancelled:
        raise
    except Exception as exc:
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled() from exc
        message = clean_error(str(exc)) or logger.last_error
        # A missing-formats warning explains a later "Requested format" error.
        hints = " ".join(w for w in logger.warnings if "challenge" in w.lower())
        raise DownloadError(message, classify_error(f"{message} {hints}")) from exc
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled()
    return info
