"""Shared fixtures: every test is isolated from the real config, data, keyring and
.env, and never touches the network unless marked ``live``."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

# Before tunegrab.config is imported (its .env path is computed at import time).
_SESSION = Path(tempfile.mkdtemp(prefix="tunegrab-tests-"))
os.environ["TUNEGRAB_CONFIG_DIR"] = str(_SESSION / "config")
os.environ["TUNEGRAB_DATA_DIR"] = str(_SESSION / "data")
os.environ["TUNEGRAB_LOG_DIR"] = str(_SESSION / "logs")
os.environ["TUNEGRAB_NO_KEYRING"] = "1"

FIXTURES = Path(__file__).parent / "fixtures"
FFMPEG = shutil.which("ffmpeg")
_KEEP = {"TUNEGRAB_CONFIG_DIR", "TUNEGRAB_DATA_DIR", "TUNEGRAB_LOG_DIR", "TUNEGRAB_NO_KEYRING"}


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TUNEGRAB_CONFIG_DIR", str(tmp_path / "_config"))
    monkeypatch.setenv("TUNEGRAB_DATA_DIR", str(tmp_path / "_data"))
    monkeypatch.setenv("TUNEGRAB_LOG_DIR", str(tmp_path / "_logs"))
    for key in list(os.environ):
        if key.startswith("SPOTIFY_") or (key.startswith("TUNEGRAB_") and key not in _KEEP):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def fixture_text(*parts: str) -> str:
    return FIXTURES.joinpath(*parts).read_text(encoding="utf-8")


requires_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not on PATH")

_CODECS = {
    "mp3": ["-c:a", "libmp3lame", "-b:a", "64k"],
    "m4a": ["-c:a", "aac", "-b:a", "64k"],
    "opus": ["-c:a", "libopus", "-b:a", "32k"],
    "flac": ["-c:a", "flac"],
}


def _ffmpeg(*args: str) -> None:
    assert FFMPEG
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", *args], check=True, timeout=60)


@pytest.fixture(scope="session")
def audio_files(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """One second of silence in every supported format."""
    if FFMPEG is None:
        pytest.skip("ffmpeg not on PATH")
    root = tmp_path_factory.mktemp("audio")
    made = {}
    for ext, codec in _CODECS.items():
        out = root / f"silence.{ext}"
        _ffmpeg("-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1", *codec, str(out))
        made[ext] = out
    return made


@pytest.fixture(scope="session")
def jpeg_bytes(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    if FFMPEG is None:
        pytest.skip("ffmpeg not on PATH")
    out = tmp_path_factory.mktemp("img") / "cover.jpg"
    _ffmpeg("-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1", str(out))
    return out.read_bytes()
