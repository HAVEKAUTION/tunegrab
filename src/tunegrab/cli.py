"""Typer app: download, get, video, info, match, search, sync, retry, fix, config, login, serve,
status, doctor."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import socket
import threading
import time
import webbrowser
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from tunegrab import __version__, storage
from tunegrab.config import (
    SECRET_KEYS,
    AudioFormat,
    PlaylistMode,
    Settings,
    VideoQuality,
    coerce_setting,
    config_file,
    keep_earlier_defaults,
    keyring_set,
    read_config_file,
    startup_warnings,
    write_config_file,
)
from tunegrab.doctor import CheckResult, Status, run_all, update_ytdlp
from tunegrab.download.convert import FORMAT_HELP
from tunegrab.download.ytdlp import BREAKAGE_HINT, DownloadError
from tunegrab.errors import TuneGrabError
from tunegrab.imports import CsvImportError, collection_from_csv
from tunegrab.library.archive import DownloadRecord
from tunegrab.library.paths import TemplateError, move_into_place, validate_template
from tunegrab.library.playlists import playlist_file
from tunegrab.logs import setup_logging
from tunegrab.matching.base import SearchError
from tunegrab.matching.ytmusic import track_from_candidate
from tunegrab.metadata.base import MetadataError
from tunegrab.models import Candidate, Collection, Job, Match, TaskStatus, Track, TrackTask
from tunegrab.pipeline import Pipeline, run_job_sync
from tunegrab.preflight import JobAborted
from tunegrab.services import Services, build_services
from tunegrab.ui import (
    LiveProgress,
    ensure_utf8_output,
    failures_table,
    fmt_duration,
    summary_table,
    tracks_table,
)
from tunegrab.urls import InvalidURLError, SpotifyRef, parse_spotify_url, parse_youtube_id
from tunegrab.video import (
    VideoError,
    VideoOptions,
    VideoPipeline,
    build_job,
    check_clip,
    parse_time,
    probe,
)

log = logging.getLogger(__name__)

app = typer.Typer(
    name="tunegrab",
    help="Download tagged audio for Spotify tracks, albums, playlists and artists "
    "(Spotify is used for metadata only; audio comes from YouTube).",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)
config_app = typer.Typer(help="Show or change settings.", no_args_is_help=True)
app.add_typer(config_app, name="config")

console = Console(highlight=False)

_STATUS_STYLE: dict[Status, tuple[str, str]] = {
    Status.OK: ("[bold green]OK[/]", "green"),
    Status.WARN: ("[bold yellow]WARN[/]", "yellow"),
    Status.FAIL: ("[bold red]FAIL[/]", "red"),
}

# --- shared options ---------------------------------------------------------------------

FormatOpt = Annotated[AudioFormat | None, typer.Option("--format", "-f", help=FORMAT_HELP)]
BitrateOpt = Annotated[
    int | None, typer.Option("--bitrate", "-b", help="Constant kbps for mp3 (default: VBR V0).")
]
OutOpt = Annotated[Path | None, typer.Option("--out", "-o", help="Output (library) folder.")]
ThreadsOpt = Annotated[int | None, typer.Option("--threads", "-t", help="Parallel downloads.")]
LyricsOpt = Annotated[bool | None, typer.Option("--lyrics/--no-lyrics", help="Embed lyrics.")]
LrcOpt = Annotated[bool | None, typer.Option("--lrc/--no-lrc", help="Write synced .lrc files.")]
PlaylistModeOpt = Annotated[
    PlaylistMode | None,
    typer.Option(
        "--playlist-mode",
        help="m3u: files go in the library + a .m3u8 playlist (no duplicates); "
        "folder: copies into Playlists/<name>/.",
    ),
]


def cli_hint(message: str) -> str:
    """The command-line next step for a shared (app-voiced) error message, if any."""
    lowered = message.lower()
    if "spotify login" in lowered:
        return "Log in with: tunegrab login"
    if "no match above" in lowered:
        return "Choose a YouTube video for it with: tunegrab fix"
    if "spotify developer credentials" in lowered or "spotify_client" in lowered:
        return "Setup steps: docs/SETUP.md"
    if "update yt-dlp in settings" in lowered:
        return "Update it with: tunegrab doctor --update"
    if "under youtube sign-in in settings" in lowered:
        return "Sign in with: tunegrab config set cookies_from_browser firefox"
    return ""


def _fail(message: str, code: int = 1) -> typer.Exit:
    console.print(f"[red]Error:[/] {escape(message)}")
    hint = cli_hint(message)
    if hint:
        console.print(f"[dim]{escape(hint)}[/]")
    return typer.Exit(code)


def load_settings(**overrides: Any) -> Settings:
    values = {k: v for k, v in overrides.items() if v is not None}
    for key in ("output_dir", "video_dir"):  # a folder typed in a shell is relative to it
        if key in values:
            values[key] = Path(values[key]).expanduser().resolve()
    try:
        settings = keep_earlier_defaults(Settings(**values))
        validate_template(settings.track_template)
        validate_template(settings.playlist_template)
    except TemplateError as exc:
        raise _fail(f"Bad filename template: {exc}") from exc
    except ValueError as exc:
        raise _fail(f"Invalid settings: {exc}") from exc
    return settings


@contextmanager
def services_for(settings: Settings) -> Iterator[Services]:
    try:
        services = build_services(settings)
    except TuneGrabError as exc:  # e.g. a download history from a newer TuneGrab
        raise _fail(str(exc)) from exc
    try:
        yield services
    finally:
        services.close()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"tunegrab {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Show the version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Log details to the terminal.")
    ] = False,
) -> None:
    """TuneGrab: Spotify metadata -> YouTube audio -> tagged local files."""
    ensure_utf8_output()
    setup_logging(verbose)
    ctx.call_on_close(_print_startup_warnings)


def _print_startup_warnings() -> None:
    for warning in startup_warnings:
        console.print(f"[yellow]Note:[/] {escape(warning)}")
    startup_warnings.clear()


# --- running jobs ---------------------------------------------------------------------


def _print_warnings(collection: Collection | None) -> None:
    if collection is None:
        return
    for warning in collection.warnings:
        console.print(f"[yellow]Note:[/] {escape(warning)}")


def run_jobs(services: Services, builders: list[tuple[str, Any]]) -> list[Job]:
    """Build (if needed) and run jobs with a live display. ``builders`` holds
    (label, job-or-input-string). Handles Ctrl+C."""
    finished: list[Job] = []
    display = LiveProgress(console)
    pipeline: Pipeline = services.pipeline(on_event=display.on_event)
    interrupted = False
    jobs: list[Job] = []
    try:
        for label, item in builders:
            if isinstance(item, Job):
                job = item
            else:
                try:
                    with console.status(f"Resolving {escape(label)} ..."):
                        job = pipeline.build_job(item)
                except (MetadataError, InvalidURLError, LookupError, ValueError) as exc:
                    console.print(f"[red]Could not resolve[/] {escape(label)}: {escape(str(exc))}")
                    if hint := cli_hint(str(exc)):
                        console.print(f"[dim]{escape(hint)}[/]")
                    continue
            _print_warnings(job.collection)
            if not job.tasks:
                console.print(f"[yellow]Nothing to download in[/] {escape(job.name)}")
                continue
            jobs.append(job)
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted.[/]")
        raise typer.Exit(130) from None
    with display:
        for job in jobs:
            display.start_job(job)
            if job.format and job.format != pipeline.settings.format:  # e.g. a retry
                pipeline = services.pipeline(
                    on_event=display.on_event,
                    settings=services.settings.model_copy(update={"format": job.format}),
                )
            try:
                run_job_sync(pipeline, job)
            except JobAborted as exc:
                job.error = str(exc)
                console.print(f"[red]Error:[/] {escape(str(exc))}")
                finished.append(job)
                break
            except KeyboardInterrupt:
                interrupted = True
                pipeline.cancel()
                job.cancelled = True
                for task in job.tasks:
                    if not task.status.finished:
                        task.status = TaskStatus.SKIPPED
                        task.message = "cancelled"
            finished.append(job)
            if interrupted:
                break
    if interrupted:
        console.print(
            "[yellow]Interrupted.[/] Unfinished tracks were discarded; run the same "
            "command again to resume."
        )
    if finished:
        console.print(summary_table(finished))
        failed = failures_table(finished)
        if failed is not None:
            console.print(failed)
            console.print("Run [bold]tunegrab retry[/] to try failed tracks again.")
            if any(
                t.status == TaskStatus.FAILED and "no match above" in (t.error or "")
                for job in finished
                for t in job.tasks
            ):
                console.print(
                    "Choose a YouTube video for unmatched songs with [bold]tunegrab fix[/]."
                )
            if any(
                t.status == TaskStatus.FAILED and BREAKAGE_HINT in (t.error or "")
                for job in finished
                for t in job.tasks
            ):
                console.print(
                    "YouTube refused some downloads; this usually means yt-dlp needs an "
                    "update: [bold]tunegrab doctor --update[/]"
                )
        for job in finished:
            collection = job.collection
            if (
                collection
                and collection.is_playlist_like
                and services.settings.playlist_mode == "m3u"
            ):
                path = playlist_file(services.settings.resolved_output_dir, collection.name)
                if path.exists():
                    console.print(f"Playlist file: {escape(str(path))}")
        console.print(f"Library: {escape(str(services.settings.resolved_output_dir))}")
    if interrupted:
        raise typer.Exit(130)
    return finished


def _exit_code(jobs: list[Job]) -> int:
    return 1 if any(t.status == TaskStatus.FAILED for j in jobs for t in j.tasks) else 0


def _read_inputs(inputs: list[str] | None, from_file: Path | None) -> list[str]:
    items = list(inputs or [])
    if from_file is not None and from_file.suffix.lower() == ".csv":
        return items  # handled by _csv_job
    if from_file is not None:
        try:
            lines = from_file.read_text(encoding="utf-8-sig").splitlines()
        except OSError as exc:
            raise _fail(f"Cannot read {from_file}: {exc}") from exc
        items += [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    if not items:
        raise _fail("Give at least one Spotify URL / search text, or --from-file.")
    return items


def _csv_job(services: Services, path: Path) -> Job:
    """A playlist job from an Exportify-style CSV (see tunegrab.imports)."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise _fail(f"Cannot read {path}: {exc}") from exc
    name = path.stem.replace("_", " ").strip() or "Imported songs"
    try:
        with console.status(f"Reading {escape(path.name)} ..."):
            collection = collection_from_csv(text, name, lookup=_spotify_track(services))
    except CsvImportError as exc:
        raise _fail(str(exc)) from exc
    tasks = [TrackTask(track=t, position=i) for i, t in enumerate(collection.tracks, start=1)]
    return Job(input=str(path), collection=collection, tasks=tasks)


def _spotify_track(services: Services) -> Callable[[str], Track]:
    def lookup(track_id: str) -> Track:
        return services.resolver.resolve_ref(SpotifyRef("track", track_id)).tracks[0]

    return lookup


@app.command()
def download(
    inputs: Annotated[
        list[str] | None,
        typer.Argument(help="Spotify track/album/playlist/artist URLs, or 'artist - title'."),
    ] = None,
    from_file: Annotated[
        Path | None,
        typer.Option(
            "--from-file",
            "-i",
            help="Text file with one input per line, or a CSV export (e.g. from Exportify).",
        ),
    ] = None,
    fmt: FormatOpt = None,
    bitrate: BitrateOpt = None,
    out: OutOpt = None,
    threads: ThreadsOpt = None,
    lyrics: LyricsOpt = None,
    lrc: LrcOpt = None,
    playlist_mode: PlaylistModeOpt = None,
) -> None:
    """Download everything in one or more Spotify links (the main command)."""
    is_csv = from_file is not None and from_file.suffix.lower() == ".csv"
    items = _read_inputs(inputs, from_file) if (inputs or not is_csv) else []
    settings = load_settings(
        format=fmt,
        bitrate=bitrate,
        output_dir=out,
        threads=threads,
        lyrics=lyrics,
        lrc=lrc,
        playlist_mode=playlist_mode,
    )
    with services_for(settings) as services:
        builders: list[tuple[str, Any]] = [(item, item) for item in items]
        if is_csv and from_file is not None:
            builders.append((from_file.name, _csv_job(services, from_file)))
        jobs = run_jobs(services, builders)
    raise typer.Exit(_exit_code(jobs))


@app.command()
def get(
    query: Annotated[
        str, typer.Argument(help="What to search for, e.g. 'Daft Punk - One More Time'.")
    ],
    fmt: FormatOpt = None,
    bitrate: BitrateOpt = None,
    out: OutOpt = None,
    lyrics: LyricsOpt = None,
) -> None:
    """Search YouTube Music and download the top result."""
    settings = load_settings(format=fmt, bitrate=bitrate, output_dir=out, lyrics=lyrics)
    with services_for(settings) as services:
        jobs = run_jobs(services, [(query, query)])
    for job in jobs:
        for task in job.tasks:
            if task.path:
                console.print(f"Saved: [bold]{escape(task.path)}[/]")
    raise typer.Exit(_exit_code(jobs))


@app.command()
def video(
    url: Annotated[
        str, typer.Argument(help="Video or playlist link: YouTube or any site yt-dlp supports.")
    ],
    audio: Annotated[
        bool, typer.Option("--audio", "-a", help="Only the audio (in --format).")
    ] = False,
    quality: Annotated[
        VideoQuality | None,
        typer.Option("--quality", "-q", help="Highest resolution: best, 2160 ... 360."),
    ] = None,
    start: Annotated[
        str | None, typer.Option("--start", "-s", help="Clip start, e.g. 1:23 (single video).")
    ] = None,
    end: Annotated[
        str | None, typer.Option("--end", "-e", help="Clip end, e.g. 2:05 (single video).")
    ] = None,
    fmt: FormatOpt = None,
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help="Video folder (default: <library>/Videos).")
    ] = None,
) -> None:
    """Download a video (MP4), a clip of one, or just its audio."""
    settings = load_settings(format=fmt, video_quality=quality, video_dir=out)
    try:
        clip_start, clip_end = parse_time(start), parse_time(end)
        with console.status("Looking up the link ..."):
            found = probe(url, settings.cookies_from_browser)
        if (clip_start is not None or clip_end is not None) and found.kind != "video":
            raise VideoError("--start/--end work on a single video, not a playlist.")
        duration = found.entries[0].duration_s if found.kind == "video" else None
        clip_start, clip_end = check_clip(clip_start, clip_end, duration)
    except (VideoError, DownloadError) as exc:
        raise _fail(str(exc)) from exc
    options = VideoOptions(
        mode="audio" if audio else "video",
        quality=settings.video_quality,
        start=clip_start,
        end=clip_end,
    )
    job, entries = build_job(found, options)
    display = LiveProgress(console)
    with services_for(settings) as services, display:
        pipeline = VideoPipeline(
            settings, options, entries, on_event=display.on_event, archive=services.archive
        )
        display.start_job(job)
        try:
            asyncio.run(pipeline.run(job))
        except JobAborted as exc:
            raise _fail(str(exc)) from exc
        except KeyboardInterrupt:
            pipeline.cancel()
            console.print("[yellow]Interrupted.[/]")
            raise typer.Exit(130) from None
    console.print(summary_table([job]))
    failed = failures_table([job])
    if failed is not None:
        console.print(failed)
    for task in job.tasks:
        if task.path and task.status == TaskStatus.DONE:
            detail = f" [dim]({escape(task.source)})[/]" if task.source else ""
            console.print(f"Saved: [bold]{escape(task.path)}[/]{detail}")
    raise typer.Exit(_exit_code([job]))


@app.command()
def info(url: Annotated[str, typer.Argument(help="Spotify URL or URI.")]) -> None:
    """Show the tracks behind a Spotify link (no downloading)."""
    settings = load_settings()
    with services_for(settings) as services:
        try:
            with console.status("Fetching from Spotify ..."):
                collection = services.resolver.resolve(url)
        except (MetadataError, InvalidURLError) as exc:
            raise _fail(str(exc)) from exc
    console.print(tracks_table(collection))
    _print_warnings(collection)
    total = sum(t.duration_s for t in collection.tracks)
    summary = f"{len(collection.tracks)} tracks, {fmt_duration(total)} (via {collection.source})"
    if collection.total and collection.total != len(collection.tracks):
        summary += f"; Spotify lists {collection.total}"
    console.print(summary)
    if collection.source == "spotify-embed" and any(t.album is None for t in collection.tracks):
        console.print("[dim]Album details are looked up per track when downloading.[/]")


@app.command()
def match(
    url: Annotated[str, typer.Argument(help="Spotify URL or URI.")],
    threshold: Annotated[float | None, typer.Option(help="Acceptance score (0-100).")] = None,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Ignore cached matches.")] = False,
) -> None:
    """Dry run: show which YouTube result each track would use, and why."""
    settings = load_settings(match_threshold=threshold)
    with services_for(settings) as services:
        try:
            with console.status("Fetching from Spotify ..."):
                collection = services.resolver.resolve(url)
        except (MetadataError, InvalidURLError) as exc:
            raise _fail(str(exc)) from exc
        _print_warnings(collection)

        def work(index: int) -> Match:
            track = services.resolver.enrich(collection.tracks[index])
            return services.matcher.match(track, use_cache=not no_cache)

        results: list[Match | None] = [None] * len(collection.tracks)
        with (
            console.status("Matching ...") as status,
            concurrent.futures.ThreadPoolExecutor(settings.threads) as pool,
        ):
            futures = {pool.submit(work, i): i for i in range(len(collection.tracks))}
            for done, future in enumerate(concurrent.futures.as_completed(futures), start=1):
                results[futures[future]] = future.result()
                status.update(f"Matching ... {done}/{len(futures)}")

    table = Table(title=f"Matches for {escape(collection.name)}", title_justify="left")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Spotify track")
    table.add_column("Chosen YouTube result")
    table.add_column("Link", no_wrap=True)
    table.add_column("Score", justify="right")
    table.add_column("Why", style="dim", overflow="fold")
    accepted = 0
    for position, result in enumerate(results, start=1):
        assert result is not None
        ok = result.accepted(settings.match_threshold)
        accepted += ok
        cand = result.candidate
        chosen = f"{', '.join(cand.artists)} - {cand.title}" if cand else "-"
        reason = result.reason + (" (cached)" if result.cached else "")
        table.add_row(
            str(position),
            escape(result.track.display),
            escape(chosen),
            cand.url if cand else "",
            f"{result.score:.0f}",
            escape(reason),
            style=None if ok else "red",
        )
    console.print(table)
    console.print(f"{accepted}/{len(results)} above threshold {settings.match_threshold:.0f}.")


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Search text.")],
    limit: Annotated[int, typer.Option(help="How many results to show.")] = 10,
    fmt: FormatOpt = None,
    out: OutOpt = None,
    videos: Annotated[bool, typer.Option("--videos", help="Include music videos.")] = False,
) -> None:
    """Search YouTube Music, pick a result interactively, and download it."""
    settings = load_settings(format=fmt, output_dir=out)
    with services_for(settings) as services:
        candidates: list[Candidate] = []
        try:
            with console.status("Searching ..."):
                candidates += services.songs.search_query(query)[:limit]
                if videos or not candidates:
                    candidates += services.videos.search_query(query)[:limit]
        except SearchError as exc:
            raise _fail(str(exc)) from exc
        if not candidates:
            raise _fail("No results.")
        table = Table(title=f"Results for {escape(query)}", title_justify="left")
        for column in ("#", "Title", "Artists", "Album", "Time", "Type"):
            table.add_column(column)
        for n, cand in enumerate(candidates, start=1):
            table.add_row(
                str(n),
                escape(cand.title),
                escape(", ".join(cand.artists)),
                escape(cand.album or ""),
                fmt_duration(cand.duration_s),
                cand.source.replace("ytmusic_", ""),
            )
        console.print(table)
        choice = typer.prompt("Download which number? (0 to cancel)", type=int, default=1)
        if choice <= 0 or choice > len(candidates):
            console.print("Cancelled.")
            raise typer.Exit()
        chosen = candidates[choice - 1]
        track = track_from_candidate(chosen)
        selected = Match(track=track, candidate=chosen, score=100.0, reason="picked by you")
        job = Job(
            input=query,
            collection=Collection(kind="search", name=track.display, tracks=[track]),
            tasks=[TrackTask(track=track, position=1, match=selected)],
        )
        jobs = run_jobs(services, [(track.display, job)])
    raise typer.Exit(_exit_code(jobs))


@app.command()
def sync(
    url: Annotated[str, typer.Argument(help="Spotify playlist URL.")],
    remove: Annotated[
        bool, typer.Option("--remove", help="Delete local tracks no longer in the playlist.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask before deleting.")] = False,
    fmt: FormatOpt = None,
    out: OutOpt = None,
    threads: ThreadsOpt = None,
    playlist_mode: PlaylistModeOpt = None,
) -> None:
    """Download new tracks of a playlist; optionally remove ones that left it."""
    settings = load_settings(
        format=fmt, output_dir=out, threads=threads, playlist_mode=playlist_mode
    )
    with services_for(settings) as services:
        try:
            ref = services.resolver.parse(url)
        except (MetadataError, InvalidURLError) as exc:
            raise _fail(str(exc)) from exc
        if ref.kind not in ("playlist", "liked"):
            raise _fail("sync works with playlists (or Liked Songs).")
        playlist_id = ref.id or "liked"
        before = {t.key: t for _, t in services.archive.playlist_members(playlist_id)}
        # tracks that left on an earlier sync but weren't deleted then (no --remove)
        pending_key = f"sync_gone:{playlist_id}"
        for raw in services.archive.get_meta(pending_key) or []:
            track = Track.model_validate(raw)
            before.setdefault(track.key, track)
        try:
            with console.status("Fetching playlist ..."):
                collection = services.resolver.resolve_ref(ref)
        except MetadataError as exc:
            raise _fail(str(exc)) from exc
        collection.id = playlist_id
        pipeline = services.pipeline()
        job = Job(
            input=url,
            collection=collection,
            tasks=[TrackTask(track=t, position=i) for i, t in enumerate(collection.tracks, 1)],
        )
        jobs = run_jobs(services, [(collection.name, job)])
        current = {t.key for t in collection.tracks}
        gone = [t for key, t in before.items() if key not in current]
        services.archive.set_meta(pending_key, [t.model_dump(mode="json") for t in gone] or None)
        if not gone:
            console.print("No tracks have left the playlist.")
            raise typer.Exit(_exit_code(jobs))
        console.print(f"[yellow]{len(gone)} track(s) are no longer in the playlist:[/]")
        keep_keys = services.archive.keys_in_other_playlists(playlist_id)
        deletable: list[tuple[Path, str]] = []
        for track in gone:
            record = services.archive.get_download(track.key)
            if record is None or not record.exists:
                console.print(f"  {escape(track.display)} [dim](not downloaded)[/]")
                continue
            if track.key in keep_keys:
                console.print(f"  {escape(track.display)} [dim](kept: in another playlist)[/]")
                continue
            console.print(f"  {escape(track.display)} -> {escape(str(record.path))}")
            deletable.append((record.path, track.key))
        if not remove:
            if deletable:
                console.print("Re-run with [bold]--remove[/] to delete these files.")
            raise typer.Exit(_exit_code(jobs))
        if deletable and (yes or typer.confirm(f"Delete {len(deletable)} file(s)?", default=False)):
            deleted: set[str] = set()
            for path, key in deletable:
                try:
                    path.unlink(missing_ok=True)
                    path.with_suffix(".lrc").unlink(missing_ok=True)
                except OSError as exc:
                    console.print(
                        f"[red]Couldn't delete[/] {escape(str(path))}: {exc.strerror or exc}"
                    )
                    continue
                services.archive.forget_download(key)
                deleted.add(key)
            left = [t.model_dump(mode="json") for t in gone if t.key not in deleted]
            services.archive.set_meta(pending_key, left or None)
            console.print(f"Deleted {len(deleted)} of {len(deletable)} file(s).")
            if len(deleted) < len(deletable):
                console.print("Close any program using the others and run sync again.")
                raise typer.Exit(1)
            if settings.playlist_mode == "m3u":
                pipeline.write_playlist_file(playlist_id)
    raise typer.Exit(_exit_code(jobs))


@app.command()
def retry(
    clear: Annotated[
        bool, typer.Option("--clear", help="Forget all recorded failures instead.")
    ] = False,
    threads: ThreadsOpt = None,
) -> None:
    """Re-run tracks that failed before."""
    settings = load_settings(threads=threads)
    with services_for(settings) as services:
        if clear:
            count = services.archive.clear_failures()
            console.print(f"Cleared {count} failure(s).")
            return
        failures = services.archive.open_failures()
        if not failures:
            console.print("No failed tracks to retry.")
            return
        groups: dict[tuple[str, str | None], list[Any]] = {}
        for failure in failures:
            groups.setdefault((failure.job_input, failure.format), []).append(failure)
        jobs_in: list[tuple[str, Any]] = []
        for (job_input, fmt), items in groups.items():
            first = items[0]
            collection = Collection(
                kind=first.collection_kind or "track",
                id=first.collection_id,
                name=first.collection_name or job_input,
                tracks=[f.track for f in items],
                source="retry",
            )
            tasks = [TrackTask(track=f.track, position=f.position or 0) for f in items]
            jobs_in.append(
                (
                    collection.name,
                    Job(input=job_input, collection=collection, tasks=tasks, format=fmt),
                )
            )
        console.print(f"Retrying {len(failures)} track(s) ...")
        jobs = run_jobs(services, jobs_in)
    raise typer.Exit(_exit_code(jobs))


@app.command()
def fix(
    spotify_url: Annotated[str, typer.Argument(help="Spotify track URL.")],
    youtube_url: Annotated[str, typer.Argument(help="The YouTube video to use instead.")],
    download_now: Annotated[
        bool, typer.Option("--download/--no-download", help="Download it right away.")
    ] = True,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Replace an old file without asking.")
    ] = False,
) -> None:
    """Override a bad match: always use YOUTUBE_URL for this Spotify track."""
    settings = load_settings()
    try:
        ref = parse_spotify_url(spotify_url)
        video_id = parse_youtube_id(youtube_url)
    except InvalidURLError as exc:
        raise _fail(str(exc)) from exc
    if ref.kind != "track":
        raise _fail("fix needs a Spotify *track* link.")
    with services_for(settings) as services:
        try:
            with console.status("Looking up both sides ..."):
                track = services.resolver.enrich(services.resolver.resolve_ref(ref).tracks[0])
                try:
                    candidate = services.youtube.video(video_id).model_copy(
                        update={"source": "manual"}
                    )
                except SearchError as exc:
                    log.info("video lookup failed: %s", exc)
                    candidate = Candidate(
                        video_id=video_id,
                        title=track.title,
                        artists=track.artists,
                        duration_s=round(track.duration_s),
                        source="manual",
                    )
        except MetadataError as exc:
            raise _fail(str(exc)) from exc
        services.archive.put_match(track.key, candidate, 100.0, "manual override", manual=True)
        console.print(
            f"Saved: {escape(track.display)} -> {candidate.url} ([dim]{escape(candidate.title)}[/])"
        )
        if not download_now:
            return
        record = services.archive.get_download(track.key)
        if record is not None and not record.exists:
            record = None
        if record is not None and not (
            yes or typer.confirm(f"Replace the existing file {record.path}?", default=True)
        ):
            return
        # Keep the old file aside until the new one is safely in place.
        old = _set_aside_for_replacement(record) if record is not None else None
        if old is not None:
            services.archive.forget_download(track.key)
        selected = Match(track=track, candidate=candidate, score=100.0, reason="manual override")
        job = Job(
            input=spotify_url,
            collection=Collection(kind="track", id=ref.id, name=track.display, tracks=[track]),
            tasks=[TrackTask(track=track, position=1, match=selected)],
        )
        try:
            jobs = run_jobs(services, [(track.display, job)])
        finally:
            if old is not None and record is not None:
                _finish_replacement(services, record, old, job.tasks[0].status)
    raise typer.Exit(_exit_code(jobs))


def _set_aside_for_replacement(record: DownloadRecord) -> list[tuple[Path, Path]]:
    """Rename the old song (and its .lrc) to a hidden backup name; returns the moves."""
    moves: list[tuple[Path, Path]] = []
    for path in (record.path, record.path.with_suffix(".lrc")):
        if not path.exists():
            continue
        backup = path.with_name(f".{path.name}.tunegrab-old")
        try:
            path.replace(backup)
        except OSError as exc:
            for original, moved in moves:  # put back what was already moved
                moved.replace(original)
            raise _fail(
                f"Couldn't replace {path}: {exc.strerror or exc}. If it's open in a player, "
                "close it and try again."
            ) from exc
        moves.append((path, backup))
    return moves


def _finish_replacement(
    services: Services, record: DownloadRecord, moves: list[tuple[Path, Path]], status: TaskStatus
) -> None:
    """Delete the backup after a successful replacement, or restore it after a failure."""
    if status == TaskStatus.DONE:
        for _original, backup in moves:
            try:
                backup.unlink(missing_ok=True)
            except OSError as exc:
                console.print(f"[yellow]Note:[/] couldn't remove {escape(str(backup))}: {exc}")
        return
    for original, backup in moves:
        try:
            move_into_place(backup, original)
        except OSError as exc:
            console.print(f"[red]Your old file is at {escape(str(backup))}[/] ({exc}).")
    services.archive.record_download(record.track, record.path, record.video_id, record.format)
    console.print("The new version wasn't downloaded; your old file is back in place.")


@app.command()
def status() -> None:
    """Show library / archive statistics and open failures."""
    settings = load_settings()
    with services_for(settings) as services:
        stats = services.archive.stats()
        failures = services.archive.open_failures()
        playlists = services.archive.playlists()
    table = Table(title="TuneGrab status", title_justify="left", show_header=False)
    table.add_row("Library", str(settings.resolved_output_dir))
    table.add_row("Archive", str(settings.resolved_archive_path))
    table.add_row("Downloaded tracks", str(stats["downloads"]))
    table.add_row("Cached matches", str(stats["cached_matches"]))
    table.add_row("Playlists", str(stats["playlists"]))
    table.add_row("Open failures", str(stats["open_failures"]))
    console.print(table)
    if playlists:
        pl = Table(title="Playlists", title_justify="left")
        pl.add_column("Name")
        pl.add_column("Tracks", justify="right")
        pl.add_column("Last synced")
        for row in playlists:
            pl.add_row(escape(row["name"]), str(row["tracks"]), row["synced_at"])
        console.print(pl)
    if failures:
        ft = Table(title="Failed tracks (tunegrab retry)", title_justify="left", title_style="red")
        ft.add_column("Track")
        ft.add_column("Reason", overflow="fold")
        for failure in failures:
            ft.add_row(escape(failure.track.display), escape(failure.reason))
        console.print(ft)


# --- auth --------------------------------------------------------------------------------


@app.command()
def login() -> None:
    """Log in to Spotify (PKCE) for private playlists and Liked Songs."""
    from tunegrab.metadata.spotify_api import login as spotify_login
    from tunegrab.net import NetworkError, get_client

    settings = load_settings()
    try:
        spotify_login(
            settings,
            get_client(),
            on_url=lambda u: console.print(
                f"Opening your browser to log in. If nothing happens, open:\n{u}"
            ),
        )
    except NetworkError as exc:
        raise _fail(f"Couldn't reach Spotify: {exc}. Check your connection and try again.") from exc
    except (MetadataError, RuntimeError, OSError) as exc:
        raise _fail(str(exc)) from exc
    console.print("[green]Logged in.[/] Private playlists and Liked Songs are now available.")


@app.command()
def logout() -> None:
    """Forget the stored Spotify login."""
    from tunegrab.metadata.spotify_api import TokenStore

    try:
        TokenStore().clear()
    except RuntimeError as exc:
        raise _fail(str(exc)) from exc
    console.print("Logged out.")


# --- config ------------------------------------------------------------------------------


def _mask(key: str, value: Any) -> str:
    if value is None:
        return "[dim]-[/]"
    if key in SECRET_KEYS:
        return "[dim]set (hidden)[/]"
    if hasattr(value, "get_secret_value"):
        return "[dim]set (hidden)[/]"
    return escape(str(value))


@config_app.command("show")
def config_show() -> None:
    """Show the effective settings and where the config file lives."""
    settings = load_settings()
    stored = read_config_file()
    table = Table(title="Settings", title_justify="left")
    table.add_column("Key")
    table.add_column("Value", overflow="fold")
    table.add_column("In config file", justify="center")
    for key in Settings.model_fields:
        table.add_row(key, _mask(key, getattr(settings, key)), "yes" if key in stored else "")
    console.print(table)
    console.print(f"Config file: {escape(str(config_file()))}")
    console.print(f"Archive:     {escape(str(settings.resolved_archive_path))}")
    console.print(
        "[dim]Precedence: CLI flags > env vars > .env > keyring > config file > defaults.[/]"
    )


@config_app.command("set")
def config_set(
    key: Annotated[str, typer.Argument(help="Setting name (see `tunegrab config show`).")],
    value: Annotated[str, typer.Argument(help="New value ('none' to clear).")],
) -> None:
    """Change a setting (saved to the config file; the client secret goes to the keyring)."""
    try:
        coerced = coerce_setting(key, value)
    except KeyError as exc:
        raise _fail(f"Unknown setting {key!r}. See `tunegrab config show`.") from exc
    except ValueError as exc:
        raise _fail(f"Invalid value for {key}: {exc}") from exc
    if key in ("track_template", "playlist_template") and coerced:
        try:
            validate_template(str(coerced))
        except TemplateError as exc:
            raise _fail(str(exc)) from exc
    if key in SECRET_KEYS:
        try:
            keyring_set(key, coerced or None)
        except RuntimeError as exc:
            raise _fail(f"{exc}. Put SPOTIFY_CLIENT_SECRET in a .env file instead.") from exc
        console.print(f"Stored {key} in the OS keyring.")
        return
    stored = read_config_file()
    if coerced is None:
        stored.pop(key, None)
    else:
        stored[key] = coerced
    path = write_config_file(stored)
    console.print(f"{key} = {_mask(key, coerced)}  [dim]({escape(str(path))})[/]")


@config_app.command("unset")
def config_unset(key: Annotated[str, typer.Argument(help="Setting name.")]) -> None:
    """Remove a setting from the config file (back to the default)."""
    if key in SECRET_KEYS:
        try:
            keyring_set(key, None)
        except RuntimeError as exc:
            raise _fail(str(exc)) from exc
        console.print(f"Removed {key} from the keyring.")
        return
    stored = read_config_file()
    if key not in stored:
        console.print(f"{key} is not set in the config file.")
        return
    stored.pop(key)
    write_config_file(stored)
    console.print(f"Removed {key}.")


@config_app.command("path")
def config_path() -> None:
    """Print the config file location."""
    console.print(str(config_file()))


# --- web UI ------------------------------------------------------------------------------


@app.command()
def serve(
    host: Annotated[
        str,
        typer.Option(help="Interface to bind (keep 127.0.0.1 unless you know what you are doing)."),
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port.")] = 8765,
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Open a browser.")] = True,
) -> None:
    """Start the local web UI."""
    import uvicorn

    from tunegrab.web.app import create_app

    url = f"http://{host}:{port}/"
    if _port_open(host, port):
        if _is_tunegrab(url):
            console.print(f"TuneGrab is already running on [bold]{url}[/]")
            if open_browser:
                webbrowser.open(url)
            return
        raise _fail(f"Port {port} is already used by another program. Try --port {port + 1}.")
    settings = load_settings()
    try:
        web_app = create_app(settings, allowed_hosts={host.lower()})
    except TuneGrabError as exc:
        raise _fail(str(exc)) from exc
    console.print(f"TuneGrab web UI on [bold]{url}[/] (Ctrl+C to stop)")
    if host not in ("127.0.0.1", "localhost", "::1"):
        console.print(
            "[yellow]Warning:[/] the UI has no login; anyone who can reach this address can use it."
        )
    if open_browser:
        # open the page only once the server answers, or the browser shows "can't connect"
        threading.Thread(target=_open_when_ready, args=(host, port, url), daemon=True).start()
    uvicorn.run(web_app, host=host, port=port, log_level="warning")


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _is_tunegrab(url: str) -> bool:
    try:
        return bool(httpx.get(url + "api/health", timeout=2).json().get("ok"))
    except (httpx.HTTPError, ValueError):
        return False


def _open_when_ready(host: str, port: int, url: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_open(host, port):
            webbrowser.open(url)
            return
        time.sleep(0.25)


# --- doctor ------------------------------------------------------------------------------


def render_checks(results: list[CheckResult]) -> Table:
    table = Table(title="tunegrab doctor", title_justify="left")
    table.add_column("Status", no_wrap=True)
    table.add_column("Check", no_wrap=True)
    table.add_column("Detail", overflow="fold")
    for result in results:
        badge, colour = _STATUS_STYLE[result.status]
        detail = escape(result.detail)
        if result.hint and result.status is not Status.OK:
            detail += f"\n[dim]{escape(result.hint)}[/]"
        table.add_row(badge, f"[{colour}]{escape(result.name)}[/]", detail)
    return table


@app.command()
def clean(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", "-n", help="Only show what would be freed.")
    ] = False,
    uv_cache: Annotated[
        bool,
        typer.Option("--uv-cache", help="Also prune uv's package cache (shared by uv projects)."),
    ] = False,
) -> None:
    """Free disk space: unfinished downloads, old logs, the yt-dlp cache; compact history.

    Never touches your music, settings or download history. Don't run it while TuneGrab
    is downloading."""
    settings = load_settings()
    with console.status("Measuring ..."):
        items = storage.report(settings, include_uv=uv_cache)
    table = Table(title="TuneGrab on disk (besides your music)", title_justify="left")
    table.add_column("What")
    table.add_column("Size", justify="right")
    table.add_column("Where", overflow="fold")
    for item in items:
        note = f"\n[dim]{escape(item.note)}[/]" if item.note else ""
        table.add_row(escape(item.label), storage.human_size(item.size), escape(item.path) + note)
    console.print(table)
    if dry_run:
        return
    keys = [i.key for i in items if i.cleanable]
    with console.status("Cleaning ..."):
        results = storage.clean(settings, keys)
    freed = sum(r.freed for r in results)
    for result in results:
        console.print(f"  {escape(result.key)}: {escape(result.detail)}")
    console.print(f"[green]Freed {storage.human_size(freed)}.[/]")


@app.command()
def doctor(
    update: Annotated[bool, typer.Option("--update", help="Upgrade yt-dlp first.")] = False,
) -> None:
    """Check that Python, ffmpeg, Deno, yt-dlp, the output dir and credentials are ready."""
    if update:
        with console.status("Upgrading yt-dlp ..."):
            ok, output = update_ytdlp()
        console.print(escape(output) if output else "")
        if not ok:
            raise _fail("yt-dlp upgrade failed (see above).")
        console.print("[green]yt-dlp upgraded.[/] Restart TuneGrab to use the new version.")
    results = run_all(load_settings())
    console.print(render_checks(results))
    failed = [r for r in results if r.status is Status.FAIL]
    if failed:
        console.print(f"[red]{len(failed)} required check(s) failed.[/]")
        raise typer.Exit(code=1)
    console.print("[green]All required checks passed.[/]")
