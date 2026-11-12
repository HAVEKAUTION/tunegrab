"""Rich terminal UI: live progress for jobs, and summary / track tables."""

from __future__ import annotations

import contextlib
import sys
import threading
from collections.abc import Iterable

from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from tunegrab.models import Collection, Job, TaskStatus, TrackTask

STATUS_STYLE = {
    TaskStatus.QUEUED: "dim",
    TaskStatus.RESOLVING: "cyan",
    TaskStatus.MATCHING: "cyan",
    TaskStatus.DOWNLOADING: "blue",
    TaskStatus.CONVERTING: "magenta",
    TaskStatus.TAGGING: "magenta",
    TaskStatus.DONE: "green",
    TaskStatus.SKIPPED: "yellow",
    TaskStatus.FAILED: "red",
}


def ensure_utf8_output() -> None:
    """Windows consoles/pipes often default to a legacy code page; song titles don't."""
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower()
        if encoding.replace("-", "") != "utf8" and hasattr(stream, "reconfigure"):
            with contextlib.suppress(OSError, ValueError):
                stream.reconfigure(encoding="utf-8", errors="replace")


def fmt_duration(seconds: float) -> str:
    total = round(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _short(text: str, width: int = 48) -> str:
    return text if len(text) <= width else text[: width - 3] + "..."


class LiveProgress:
    """Overall bar + one row per active track; finished tracks scroll above."""

    def __init__(self, console: Console, show_skipped: bool = False) -> None:
        self.console = console
        self.show_skipped = show_skipped
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(bar_width=24),
            TaskProgressColumn(),
            TextColumn("[dim]{task.fields[status]}"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
            refresh_per_second=8,
        )
        self._overall: TaskID | None = None
        self._rows: dict[str, TaskID] = {}
        self._finished: set[str] = set()
        self._lock = threading.Lock()

    def __enter__(self) -> LiveProgress:
        self.progress.start()
        return self

    def __exit__(self, *exc: object) -> None:
        with self._lock:
            for row in self._rows.values():
                self.progress.remove_task(row)
            self._rows.clear()
        self.progress.stop()

    def start_job(self, job: Job) -> None:
        with self._lock:
            if self._overall is not None:
                self.progress.remove_task(self._overall)
            self._finished = set()
            self._overall = self.progress.add_task(
                f"[bold]{escape(_short(job.name, 40))}",
                total=max(1, len(job.tasks)),
                status=f"{len(job.tasks)} tracks",
            )

    def on_event(self, job: Job, task: TrackTask | None) -> None:
        if task is None:
            return
        with self._lock:
            if task.status.finished:
                self._finish(task)
            else:
                self._update(task)

    def _update(self, task: TrackTask) -> None:
        row = self._rows.get(task.id)
        description = escape(_short(task.track.display))
        status = task.message or task.status.value
        if task.status == TaskStatus.DOWNLOADING:
            completed = int(task.progress * 100)
        elif task.status in (TaskStatus.CONVERTING, TaskStatus.TAGGING):
            completed = 100
        else:
            completed = 0
        if row is None:
            self._rows[task.id] = self.progress.add_task(
                description, total=100, completed=completed, status=status
            )
        else:
            self.progress.update(row, completed=completed, status=status)

    def _finish(self, task: TrackTask) -> None:
        if task.id in self._finished:
            return
        self._finished.add(task.id)
        row = self._rows.pop(task.id, None)
        if row is not None:
            self.progress.remove_task(row)
        if self._overall is not None:
            self.progress.advance(self._overall)
        name = escape(task.track.display)
        if task.status == TaskStatus.DONE:
            self.console.print(f"[green]done[/]    {name}")
        elif task.status == TaskStatus.FAILED:
            self.console.print(f"[red]failed[/]  {name}: [dim]{escape(task.error or '')}[/]")
        elif self.show_skipped or task.message != "already downloaded":
            self.console.print(f"[yellow]skipped[/] {name} [dim]({escape(task.message)})[/]")


def summary_table(jobs: Iterable[Job]) -> Table:
    table = Table(title="Summary", title_justify="left")
    table.add_column("Job")
    for label, style in (("done", "green"), ("skipped", "yellow"), ("failed", "red")):
        table.add_column(label, justify="right", style=style)
    for job in jobs:
        counts = job.counts()
        table.add_row(
            escape(_short(job.name, 50)),
            str(counts["done"]),
            str(counts["skipped"]),
            str(counts["failed"]),
        )
    return table


def failures_table(jobs: Iterable[Job]) -> Table | None:
    rows = [(job, t) for job in jobs for t in job.tasks if t.status == TaskStatus.FAILED]
    if not rows:
        return None
    table = Table(title="Failed tracks", title_justify="left", title_style="red")
    table.add_column("Track")
    table.add_column("Stage", style="dim")
    table.add_column("Reason", overflow="fold")
    for _job, task in rows:
        table.add_row(escape(task.track.display), task.failed_stage or "", escape(task.error or ""))
    return table


def tracks_table(collection: Collection) -> Table:
    title = f"{collection.kind.title()}: {collection.name}"
    if collection.owner:
        title += f" - {collection.owner}"
    table = Table(title=escape(title), title_justify="left")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Title")
    table.add_column("Artists")
    table.add_column("Album")
    table.add_column("Time", justify="right")
    for position, track in enumerate(collection.tracks, start=1):
        table.add_row(
            str(position),
            escape(track.title) + (" [dim](E)[/]" if track.explicit else ""),
            escape(", ".join(track.artists)),
            escape(track.album or ""),
            fmt_duration(track.duration_s),
        )
    return table
