"""What the music and video pipelines share: events, status changes, cancel/abort, pacing
under YouTube's limit, the retry loop, and turning a final error into a failed task.

Each pipeline supplies only the work of one attempt and which errors are worth retrying.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from tunegrab.config import Settings
from tunegrab.download.ytdlp import DownloadCancelled, describe_failure
from tunegrab.errors import TuneGrabError
from tunegrab.models import Job, TaskStatus, TrackTask
from tunegrab.pacing import Pacer, youtube_pacer
from tunegrab.preflight import DISK_FULL, is_disk_full

log = logging.getLogger(__name__)

EventCallback = Callable[[Job, TrackTask | None], None]


class TaskFailed(TuneGrabError):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class Cancelled(Exception):
    """The user cancelled the job (or it was stopped for everyone, e.g. a full disk)."""


def unexpected_message(exc: BaseException) -> str:
    """A plain sentence for an error nobody planned for (a bug); details go to the log."""
    detail = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else ""
    name = type(exc).__name__
    what = f"{name}: {detail}" if detail else name
    return f"Something unexpected went wrong ({what}). The details are in the TuneGrab log."


def failure_message(exc: BaseException) -> str:
    """The sentence shown on a failed song or video."""
    if is_disk_full(exc):
        return DISK_FULL
    if isinstance(exc, TuneGrabError | OSError):
        return describe_failure(exc)
    return unexpected_message(exc)


class TaskRunner:
    """Mixin for the pipelines. Subclasses (dataclasses) provide these attributes."""

    settings: Settings
    on_event: EventCallback | None
    cancel_event: threading.Event
    abort_reason: str | None
    retry_backoff: float
    pacer: Pacer | None

    # --- job control ------------------------------------------------------------------
    def cancel(self) -> None:
        self.cancel_event.set()

    def abort(self, reason: str) -> None:
        """Stop the whole job because of ``reason`` (the songs not done are failed, so
        Retry picks them up once the cause is fixed)."""
        if self.abort_reason is None:
            self.abort_reason = reason
            log.warning("stopping the job: %s", reason)
        self.cancel_event.set()

    # --- events -----------------------------------------------------------------------
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

    # --- pacing -----------------------------------------------------------------------
    def _pace(self, job: Job, task: TrackTask) -> None:
        """Wait for a start slot under YouTube's hourly budget (see tunegrab.pacing)."""
        pacer = self.pacer or youtube_pacer(bool(self.settings.cookies_from_browser))
        wait = pacer.reserve()
        if wait <= 0:
            return
        task.message = f"Pacing downloads to stay under YouTube's limit: starting in {wait:.0f} s"
        self._emit(job, task)
        if self.cancel_event.wait(wait):
            raise Cancelled()

    # --- attempts and failures ------------------------------------------------------------
    def _run_attempts(
        self,
        job: Job,
        task: TrackTask,
        attempt: Callable[[], None],
        retryable: Callable[[Exception], bool],
    ) -> None:
        """Run ``attempt`` up to ``settings.retries + 1`` times with growing back-off.
        Raises the last error (or :class:`Cancelled`)."""
        attempts = self.settings.retries + 1
        for number in range(1, attempts + 1):
            task.attempts = number
            try:
                attempt()
                return
            except (Cancelled, DownloadCancelled):
                raise
            except Exception as exc:
                if number == attempts or is_disk_full(exc) or not retryable(exc):
                    raise
                log.info("retrying %s after: %s", task.track.display, exc)
            task.message = f"retry {number}/{attempts - 1}"
            self._emit(job, task)
            if self.cancel_event.wait(self.retry_backoff * number):
                raise Cancelled()

    def _fail_with(self, job: Job, task: TrackTask, exc: BaseException) -> None:
        """The task's last attempt failed with ``exc``; a full disk stops the whole job."""
        if is_disk_full(exc):
            self.abort(DISK_FULL)
        elif not isinstance(exc, TuneGrabError | OSError):
            log.error("%s failed unexpectedly", task.track.display, exc_info=exc)
        self._fail_task(job, task, failure_message(exc))

    def _fail_task(self, job: Job, task: TrackTask, error: str) -> None:
        stage = task.status.value if not task.status.finished else None
        task.failed_stage = stage
        task.error = error
        log.warning("failed: %s at %s: %s", task.track.display, stage, task.error)
        try:
            self._record_failure(job, task)
        except Exception:
            log.exception("could not record failure")
        self._set(job, task, TaskStatus.FAILED, task.error)

    def _record_failure(self, job: Job, task: TrackTask) -> None:
        """Remember the failure so Retry finds it after a restart (music only for now)."""
