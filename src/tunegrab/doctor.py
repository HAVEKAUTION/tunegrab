"""Environment checks behind ``tunegrab doctor``.

Each check is a small function returning a :class:`CheckResult`, so it can be
unit-tested by patching ``shutil.which`` / ``subprocess.run``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path

from tunegrab.config import Settings

MIN_PYTHON = (3, 11)
_VERSION_TIMEOUT_S = 10


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


def check_deno() -> CheckResult:
    return check_executable(
        "deno",
        "Deno",
        ["--version"],
        "yt-dlp needs a JS runtime for YouTube. Install Deno "
        "(Windows: irm https://deno.land/install.ps1 | iex) and restart your terminal.",
    )


def check_ytdlp() -> CheckResult:
    """yt-dlp must be installed; report its version (old versions break often)."""
    try:
        found = package_version("yt-dlp")
    except PackageNotFoundError:
        return CheckResult("yt-dlp", Status.FAIL, "not installed", "Run: uv sync")
    return CheckResult("yt-dlp", Status.OK, found, "If YouTube breaks, run: uv add yt-dlp@latest")


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
            [uv, "sync", "--directory", str(root)],
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
        "Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env (see docs/SETUP.md).",
    )


def run_all(settings: Settings) -> list[CheckResult]:
    return [
        check_python(),
        check_ffmpeg(),
        check_deno(),
        check_ytdlp(),
        check_ejs(),
        check_output_dir(settings.output_dir),
        check_spotify_credentials(settings),
    ]
