"""Unit tests for the individual doctor checks. No real executables are run."""

from __future__ import annotations

import subprocess
from datetime import date
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tunegrab import doctor
from tunegrab.config import Settings
from tunegrab.doctor import Status


def _completed(
    stdout: str = "", stderr: str = "", code: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr=stderr)


@pytest.fixture
def no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make Settings ignore the developer's real env vars."""
    for key in (
        "SPOTIFY_CLIENT_ID",
        "SPOTIFY_CLIENT_SECRET",
        "SPOTIFY_REDIRECT_URI",
        "TUNEGRAB_OUTPUT_DIR",
    ):
        monkeypatch.delenv(key, raising=False)


# --- Python ----------------------------------------------------------------


@pytest.mark.parametrize("info", [(3, 11, 0), (3, 12, 4), (4, 0, 0)])
def test_python_ok(info: tuple[int, int, int]) -> None:
    result = doctor.check_python(info)
    assert result.status is Status.OK
    assert result.detail == ".".join(map(str, info))


def test_python_too_old() -> None:
    result = doctor.check_python((3, 10, 12))
    assert result.status is Status.FAIL
    assert "3.11+" in result.detail
    assert result.hint


def test_python_defaults_to_running_interpreter() -> None:
    assert doctor.check_python().status is Status.OK


# --- executables (ffmpeg / deno) -------------------------------------------


def test_ffmpeg_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    run = MagicMock()
    monkeypatch.setattr(doctor.subprocess, "run", run)

    result = doctor.check_ffmpeg()

    assert result.status is Status.FAIL
    assert result.detail == "not found on PATH"
    assert "ffmpeg" in result.hint.lower()
    run.assert_not_called()


def test_ffmpeg_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    run = MagicMock(
        return_value=_completed(
            "ffmpeg version 7.1-full_build Copyright (c) 2000-2025 the FFmpeg developers\n"
            "built with gcc\n"
        )
    )
    monkeypatch.setattr(doctor.subprocess, "run", run)

    result = doctor.check_ffmpeg()

    assert result.status is Status.OK
    assert result.detail == "ffmpeg version 7.1-full_build"
    args: list[str] = run.call_args.args[0]
    assert args == ["/usr/bin/ffmpeg", "-version"]
    assert run.call_args.kwargs["timeout"] > 0


def test_deno_present(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_which(name: str) -> str:
        seen.append(name)
        return "C:/deno/deno.exe"

    monkeypatch.setattr(doctor.shutil, "which", fake_which)
    monkeypatch.setattr(
        doctor.subprocess, "run", MagicMock(return_value=_completed("deno 2.5.0 (stable)\nv8\n"))
    )

    result = doctor.check_js_runtime()

    assert seen == ["deno", "deno"]  # probe, then run
    assert result.status is Status.OK
    assert result.name == "JS runtime"
    assert result.detail == "Deno: deno 2.5.0 (stable)"


def test_js_runtime_missing_hint_mentions_js_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    result = doctor.check_js_runtime()
    assert result.status is Status.FAIL
    assert "JS runtime" in result.hint and "none found" in result.detail


def test_js_runtime_falls_back_to_node_when_deno_is_too_old(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        doctor.shutil, "which", lambda name: f"/bin/{name}" if name in ("deno", "node") else None
    )
    outputs = {"/bin/deno": "deno 2.1.4 (stable)", "/bin/node": "v24.21.0"}
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda args, **_kw: _completed(outputs[args[0]]),
    )
    result = doctor.check_js_runtime()
    assert result.status is Status.OK and result.detail == "Node.js: v24.21.0"


def test_js_runtime_all_too_old(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/bin/x" if name == "node" else None)
    monkeypatch.setattr(doctor.subprocess, "run", lambda *_a, **_kw: _completed("v20.11.0"))
    result = doctor.check_js_runtime()
    assert result.status is Status.FAIL and "need 22.0.0+" in result.detail


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("deno 2.9.8 (stable, release, x86_64-pc-windows-msvc)", (2, 9, 8)),
        ("v24.21.0", (24, 21, 0)),
        ("1.2", (1, 2, 0)),
        ("QuickJS version 2024-01-13", (2024, 1, 13)),
        ("no digits", None),
    ],
)
def test_parse_version(text: str, expected: tuple[int, int, int] | None) -> None:
    assert doctor.parse_version(text) == expected


def test_executable_version_on_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/bin/tool")
    monkeypatch.setattr(
        doctor.subprocess, "run", MagicMock(return_value=_completed(stderr="tool 1.0\n"))
    )
    result = doctor.check_executable("tool", "Tool", ["--version"], "hint")
    assert result.status is Status.OK
    assert result.detail == "tool 1.0"


def test_executable_empty_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/bin/tool")
    monkeypatch.setattr(doctor.subprocess, "run", MagicMock(return_value=_completed()))
    result = doctor.check_executable("tool", "Tool", ["--version"], "hint")
    assert result.status is Status.OK
    assert result.detail == "version unknown"


def test_executable_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/bin/tool")
    monkeypatch.setattr(doctor.subprocess, "run", MagicMock(return_value=_completed(code=3)))
    result = doctor.check_executable("tool", "Tool", ["--version"], "install it")
    assert result.status is Status.FAIL
    assert "code 3" in result.detail
    assert result.hint == "install it"


@pytest.mark.parametrize(
    "exc",
    [
        subprocess.TimeoutExpired(cmd="tool", timeout=10),
        PermissionError("access denied"),
    ],
)
def test_executable_fails_to_run(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/bin/tool")
    monkeypatch.setattr(doctor.subprocess, "run", MagicMock(side_effect=exc))
    result = doctor.check_executable("tool", "Tool", ["--version"], "hint")
    assert result.status is Status.FAIL
    assert "failed to run" in result.detail


# --- yt-dlp ----------------------------------------------------------------


def test_ytdlp_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "package_version", lambda _name: "2026.10.26")
    result = doctor.check_ytdlp(today=date(2026, 11, 13))
    assert result.status is Status.OK
    assert result.detail == "2026.10.26"


def test_ytdlp_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "package_version", lambda _name: "2026.6.1")
    result = doctor.check_ytdlp(today=date(2026, 11, 13))
    assert result.status is Status.WARN
    assert result.detail == "2026.6.1 (165 days old)" and "Update yt-dlp" in result.hint


def test_ytdlp_odd_version_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "package_version", lambda _name: "2026.10.26.dev0+abc")
    assert doctor.check_ytdlp(today=date(2026, 11, 13)).status is Status.OK


def test_ytdlp_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(doctor, "package_version", missing)
    result = doctor.check_ytdlp()
    assert result.status is Status.FAIL
    assert result.detail == "not installed"


# --- output dir ------------------------------------------------------------


def test_output_dir_existing_writable(tmp_path: Path) -> None:
    result = doctor.check_output_dir(tmp_path)
    assert result.status is Status.OK
    assert result.detail == str(tmp_path.resolve())
    assert list(tmp_path.iterdir()) == []  # probe file cleaned up


def test_output_dir_missing_is_not_created(tmp_path: Path) -> None:
    target = tmp_path / "music" / "library"
    result = doctor.check_output_dir(target)
    assert result.status is Status.OK
    assert "will be created" in result.detail
    assert not target.exists()


def test_output_dir_is_a_file(tmp_path: Path) -> None:
    file_path = tmp_path / "not-a-dir"
    file_path.write_text("x")
    result = doctor.check_output_dir(file_path)
    assert result.status is Status.FAIL
    assert "not a directory" in result.detail


def test_output_dir_not_writable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*_args: Any, **_kwargs: Any) -> Any:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(doctor.tempfile, "TemporaryFile", deny)
    result = doctor.check_output_dir(tmp_path)
    assert result.status is Status.FAIL
    assert "Permission denied" in result.detail


# --- Spotify credentials ---------------------------------------------------


@pytest.mark.usefixtures("no_env")
def test_spotify_creds_present() -> None:
    settings = Settings(_env_file=None, SPOTIFY_CLIENT_ID="id", SPOTIFY_CLIENT_SECRET="secret")
    result = doctor.check_spotify_credentials(settings)
    assert result.status is Status.OK
    assert result.detail == "yes"
    assert "secret" not in repr(result)


@pytest.mark.usefixtures("no_env")
@pytest.mark.parametrize(
    "values",
    [
        {},
        {"SPOTIFY_CLIENT_ID": "id"},
        {"SPOTIFY_CLIENT_SECRET": "secret"},
        {"SPOTIFY_CLIENT_ID": "", "SPOTIFY_CLIENT_SECRET": ""},
    ],
)
def test_spotify_creds_missing_is_warning_not_failure(values: dict[str, str]) -> None:
    settings = Settings(_env_file=None, **values)  # type: ignore[arg-type]
    result = doctor.check_spotify_credentials(settings)
    assert result.status is Status.WARN
    assert result.detail.startswith("no")


@pytest.mark.usefixtures("no_env")
def test_spotify_creds_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "id")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "secret")
    result = doctor.check_spotify_credentials(Settings(_env_file=None))
    assert result.status is Status.OK


# --- run_all ---------------------------------------------------------------


@pytest.mark.usefixtures("no_env")
def test_run_all_covers_every_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(doctor.subprocess, "run", MagicMock(return_value=_completed("v1\n")))
    settings = Settings(_env_file=None, TUNEGRAB_OUTPUT_DIR=str(tmp_path))  # type: ignore[arg-type]

    names = [r.name for r in doctor.run_all(settings)]

    assert names == [
        "Python",
        "ffmpeg",
        "JS runtime",
        "yt-dlp",
        "yt-dlp JS solver",
        "Output dir writable",
        "Spotify credentials",
    ]


# --- updating yt-dlp ---------------------------------------------------------


def test_update_commands_upgrade_the_lock_in_a_uv_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`uv run --locked` re-syncs the venv to uv.lock, so the lock itself must move."""
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    monkeypatch.setattr(doctor.sys, "prefix", str(tmp_path / ".venv"))
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "uv")
    lock, sync = doctor.update_commands()
    assert lock[:2] == ["uv", "lock"]
    assert ["--upgrade-package", "yt-dlp"] == lock[2:4]
    assert "yt-dlp-ejs" in lock
    assert lock[-2:] == ["--directory", str(tmp_path)]
    assert sync[:2] == ["uv", "sync"]
    # never reinstall tunegrab itself (a running server locks tunegrab.exe) or add dev tools
    assert {"--no-install-project", "--inexact", "--no-dev"} <= set(sync)


def test_update_commands_without_a_uv_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor.sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "uv")
    assert doctor.update_commands() == [
        ["uv", "pip", "install", "--python", doctor.sys.executable, "--upgrade", "yt-dlp[default]"]
    ]
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    assert doctor.update_commands() == [
        [doctor.sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp[default]"]
    ]


def test_update_ytdlp_stops_at_the_first_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "update_commands", lambda: [["first"], ["second"]])
    calls: list[list[str]] = []

    def run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return _completed(stderr="lock failed", code=1)

    monkeypatch.setattr(doctor.subprocess, "run", run)
    ok, output = doctor.update_ytdlp()
    assert not ok
    assert "lock failed" in output
    assert calls == [["first"]]


def test_update_ytdlp_reports_a_missing_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "update_commands", lambda: [["uv", "lock"]])

    def run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("uv")

    monkeypatch.setattr(doctor.subprocess, "run", run)
    ok, output = doctor.update_ytdlp()
    assert not ok
    assert output.startswith("uv lock:")
