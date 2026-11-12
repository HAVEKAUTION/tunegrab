"""Environment checks behind ``tunegrab doctor``.

Each check is a small function returning a :class:`CheckResult`, so it can be
unit-tested by patching ``shutil.which`` / ``subprocess.run``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path

from tunegrab.config import Settings
from tunegrab.metadata.base import CREDENTIALS_HELP

MIN_PYTHON = (3, 11)
_VERSION_TIMEOUT_S = 10
# yt-dlp's version is its release date; YouTube changes often enough that a build older
# than this is the most likely reason downloads break.
YTDLP_STALE_DAYS = 60

# JS runtimes yt-dlp can use to solve YouTube's challenges, in the order it prefers them,
# with the oldest version it supports: (executable, label, version args, minimum).
JS_RUNTIMES: tuple[tuple[str, str, list[str], tuple[int, int, int]], ...] = (
    ("deno", "Deno", ["--version"], (2, 3, 0)),
    ("node", "Node.js", ["--version"], (22, 0, 0)),
    ("bun", "Bun", ["--version"], (1, 2, 11)),
    ("qjs", "QuickJS", ["--help"], (2023, 12, 9)),
)
JS_RUNTIME_HINT = (
    "yt-dlp needs a JS runtime for YouTube. Install Deno "
    "(Windows: irm https://deno.land/install.ps1 | iex; macOS/Linux: "
    "curl -fsSL https://deno.land/install.sh | sh) and restart your terminal. "
    "Node.js 22+, Bun 1.2.11+ or QuickJS also work."
)
_VERSION = re.compile(r"(\d{4})-(\d{2})-(\d{2})|(\d+)\.(\d+)(?:\.(\d+))?")


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"  # works, but something optional is missing
    FAIL = "fail"  # a required piece is missing


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    detail: str
    hint: str = ""


def check_python(version_info: tuple[int, ...] | None = None) -> CheckResult:
    """Python must be 3.11 or newer."""
    info = tuple(version_info if version_info is not None else sys.version_info[:3])
    shown = ".".join(str(part) for part in info[:3])
    if info[:2] >= MIN_PYTHON:
        return CheckResult("Python", Status.OK, shown)
    return CheckResult(
        "Python",
        Status.FAIL,
        f"{shown} (need {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+)",
        "Install a newer Python: uv python install 3.12",
    )


def check_executable(name: str, label: str, version_args: list[str], hint: str) -> CheckResult:
    """An external program must be on PATH and answer its version command."""
    path = shutil.which(name)
    if path is None:
        return CheckResult(label, Status.FAIL, "not found on PATH", hint)
    try:
        proc = subprocess.run(
            [path, *version_args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_VERSION_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult(label, Status.FAIL, f"found at {path} but failed to run: {exc}", hint)
    if proc.returncode != 0:
        return CheckResult(
            label, Status.FAIL, f"found at {path} but exited with code {proc.returncode}", hint
        )
    lines = (proc.stdout or proc.stderr or "").strip().splitlines()
    first_line = lines[0].split(" Copyright")[0].strip() if lines else ""
    return CheckResult(label, Status.OK, first_line or "version unknown")


def check_ffmpeg() -> CheckResult:
    return check_executable(
        "ffmpeg",
        "ffmpeg",
        ["-version"],
        "Install ffmpeg (Windows: winget install Gyan.FFmpeg) and restart your terminal.",
    )


def parse_version(text: str) -> tuple[int, int, int] | None:
    """First version number in ``text``: ``2.9.8``, ``v24.1.0`` or a ``2024-01-13`` date."""
    found = _VERSION.search(text)
    if found is None:
        return None
    parts = found.groups()
    numbers = parts[:3] if parts[0] else parts[3:]
    return (int(numbers[0]), int(numbers[1]), int(numbers[2] or 0))


def check_js_runtime() -> CheckResult:
    """Any JS runtime yt-dlp supports (Deno preferred), new enough to be used."""
    label = "JS runtime"
    too_old: list[str] = []
    for exe, name, args, minimum in JS_RUNTIMES:
        if shutil.which(exe) is None:
            continue
        result = check_executable(exe, name, args, JS_RUNTIME_HINT)
        if result.status is not Status.OK:
            too_old.append(f"{name}: {result.detail}")
            continue
        version = parse_version(result.detail)
        if version is not None and version < minimum:
            shown = ".".join(str(n) for n in minimum)
            too_old.append(f"{name} {'.'.join(map(str, version))} (need {shown}+)")
            continue
        return CheckResult(label, Status.OK, f"{name}: {result.detail}")
    detail = "; ".join(too_old) if too_old else "none found on PATH (Deno, Node.js, Bun, QuickJS)"
    return CheckResult(label, Status.FAIL, detail, JS_RUNTIME_HINT)


def check_ytdlp(today: date | None = None) -> CheckResult:
    """yt-dlp must be installed. Its version is its release date: warn when it's old,
    since an outdated yt-dlp is the usual reason YouTube downloads start failing."""
    try:
        found = package_version("yt-dlp")
    except PackageNotFoundError:
        return CheckResult("yt-dlp", Status.FAIL, "not installed", "Run: uv sync")
    hint = 'Update it: Settings > System > "Update yt-dlp", or run: tunegrab doctor --update'
    try:
        year, month, day = (int(part) for part in found.split(".")[:3])
        released = date(year, month, day)
    except ValueError:
        return CheckResult("yt-dlp", Status.OK, found, hint)
    age = ((today or date.today()) - released).days
    if age > YTDLP_STALE_DAYS:
        return CheckResult("yt-dlp", Status.WARN, f"{found} ({age} days old)", hint)
    return CheckResult("yt-dlp", Status.OK, found, hint)


def check_ejs() -> CheckResult:
    """yt-dlp-ejs holds the scripts Deno runs to solve YouTube's JS challenges."""
    label = "yt-dlp JS solver"
    try:
        found = package_version("yt-dlp-ejs")
    except PackageNotFoundError:
        return CheckResult(
            label,
            Status.WARN,
            "yt-dlp-ejs not installed: some YouTube formats may be missing",
            'Run: uv sync (it is part of the "yt-dlp[default]" dependency)',
        )
    return CheckResult(label, Status.OK, f"yt-dlp-ejs {found}")


def _project_root() -> Path | None:
    """The source checkout this interpreter belongs to, if running via `uv run`."""
    venv = Path(sys.prefix)
    root = venv.parent
    if venv.name == ".venv" and (root / "pyproject.toml").exists() and (root / "uv.lock").exists():
        return root
    return None


def update_commands() -> list[list[str]]:
    """Commands that upgrade yt-dlp for however TuneGrab is installed."""
    uv = shutil.which("uv")
    root = _project_root()
    if uv and root is not None:
        # `uv run` re-syncs to uv.lock, so upgrade the lock rather than the venv.
        return [
            [
                uv,
                "lock",
                "--upgrade-package",
                "yt-dlp",
                "--upgrade-package",
                "yt-dlp-ejs",
                "--directory",
                str(root),
            ],
            # --no-install-project: a running server locks tunegrab.exe on Windows.
            # --inexact --no-dev: change nothing but the upgraded packages.
            [
                uv,
                "sync",
                "--no-install-project",
                "--inexact",
                "--no-dev",
                "--directory",
                str(root),
            ],
        ]
    if uv:
        return [[uv, "pip", "install", "--python", sys.executable, "--upgrade", "yt-dlp[default]"]]
    return [[sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp[default]"]]


def update_ytdlp() -> tuple[bool, str]:
    """Run the upgrade; returns (ok, combined output)."""
    output: list[str] = []
    for command in update_commands():
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"{' '.join(command)}: {exc}"
        output.append((proc.stdout + proc.stderr).strip())
        if proc.returncode != 0:
            return False, "\n".join(output)
    return True, "\n".join(output)


def check_output_dir(path: Path) -> CheckResult:
    """The output dir (or its nearest existing parent, if it doesn't exist yet) must be writable.

    Nothing is created: doctor only looks.
    """
    label = "Output dir writable"
    target = path.expanduser().resolve()
    probe = target
    while not probe.exists():
        if probe.parent == probe:
            return CheckResult(label, Status.FAIL, f"{target}: no existing parent directory")
        probe = probe.parent
    if not probe.is_dir():
        return CheckResult(label, Status.FAIL, f"{probe} exists but is not a directory")
    try:
        with tempfile.TemporaryFile(dir=probe):
            pass
    except OSError as exc:
        return CheckResult(
            label, Status.FAIL, f"{probe}: {exc.strerror or exc}", "Pick another output directory."
        )
    detail = str(target) if probe == target else f"{target} (will be created)"
    return CheckResult(label, Status.OK, detail)


def check_spotify_credentials(settings: Settings) -> CheckResult:
    """Credentials are optional: without them the embed-page fallback is used."""
    label = "Spotify credentials"
    if settings.has_spotify_credentials:
        return CheckResult(label, Status.OK, "yes")
    return CheckResult(
        label,
        Status.WARN,
        "no (will use the public embed fallback)",
        "Optional: only needed for playlists over 100 songs, full artist lists and private "
        f"playlists ({CREDENTIALS_HELP}).",
    )


def run_all(settings: Settings) -> list[CheckResult]:
    return [
        check_python(),
        check_ffmpeg(),
        check_js_runtime(),
        check_ytdlp(),
        check_ejs(),
        check_output_dir(settings.resolved_output_dir),
        check_spotify_credentials(settings),
    ]
