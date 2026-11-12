"""What TuneGrab keeps on disk besides your music, and cleaning it up (``tunegrab clean``).

TuneGrab's own data is small: settings, the download-history database and a rotating log
(2 MB x 3 backups) in the per-user app-data folders. The things that grow are
temporary download folders left by a crash, old log backups, free pages in the database,
and caches of the tools around it (yt-dlp's cache, uv's package cache).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from tunegrab.config import Settings, config_dir, log_dir

TMP_DIR_NAME = ".tunegrab-tmp"
UV_TIMEOUT_S = 600


@dataclass(frozen=True)
class StorageItem:
    key: str
    label: str
    path: str
    size: int  # bytes
    cleanable: bool  # can `clean` free this?
    note: str = ""

    def payload(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "path": self.path,
            "size": self.size,
            "cleanable": self.cleanable,
            "note": self.note,
        }


def size_of(path: Path) -> int:
    """Total size of a file or folder (0 if missing); never follows links."""
    try:
        if path.is_symlink():
            return 0
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return 0
    total = 0
    for root, dirs, names in os.walk(path, followlinks=False):
        for name in names:
            with contextlib.suppress(OSError):
                total += (Path(root) / name).lstat().st_size
        dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
    return total


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def tmp_dirs(settings: Settings) -> list[Path]:
    roots = {settings.resolved_output_dir, settings.resolved_video_dir}
    return sorted(root / TMP_DIR_NAME for root in roots)


def log_backups() -> list[Path]:
    folder = log_dir()
    return sorted(folder.glob("tunegrab.log.*")) if folder.exists() else []


def ytdlp_cache_dir() -> Path:
    """Where yt-dlp keeps its cache by default (TuneGrab 0.4+ turns it off, but older
    versions and a separate yt-dlp install use it)."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "yt-dlp"


def uv_cache_dir() -> Path | None:
    uv = shutil.which("uv")
    if uv is None:
        return None
    try:
        proc = subprocess.run(
            [uv, "cache", "dir"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = proc.stdout.strip()
    return Path(text) if proc.returncode == 0 and text else None


def database_free_bytes(path: Path) -> int:
    """Space VACUUM would give back."""
    if not path.exists():
        return 0
    try:
        with sqlite3.connect(path, timeout=5) as db:
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            free = db.execute("PRAGMA freelist_count").fetchone()[0]
        return int(page_size) * int(free)
    except sqlite3.Error:
        return 0


def report(settings: Settings, include_uv: bool = False) -> list[StorageItem]:
    """Sizes of everything TuneGrab puts on disk (music excluded)."""
    db = settings.resolved_archive_path
    db_files = [db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")]
    logs = log_dir()
    backups = log_backups()
    items = [
        StorageItem(
            "tmp",
            "Unfinished downloads",
            ", ".join(str(p) for p in tmp_dirs(settings)),
            sum(size_of(p) for p in tmp_dirs(settings)),
            True,
            "Left behind if TuneGrab was closed mid-download.",
        ),
        StorageItem(
            "logs",
            "Old log files",
            str(logs),
            sum(size_of(p) for p in backups),
            True,
            f"The current log ({human_size(size_of(logs / 'tunegrab.log'))}) is kept.",
        ),
        StorageItem(
            "database",
            "Download history",
            str(db),
            sum(size_of(p) for p in db_files),
            True,
            f"{human_size(database_free_bytes(db))} can be compacted; nothing is deleted.",
        ),
        StorageItem(
            "settings",
            "Settings",
            str(config_dir()),
            size_of(config_dir() / "config.toml"),
            False,
        ),
        StorageItem(
            "ytdlp_cache",
            "yt-dlp cache",
            str(ytdlp_cache_dir()),
            size_of(ytdlp_cache_dir()),
            True,
            "Not used by TuneGrab any more; yt-dlp rebuilds it if it needs it.",
        ),
    ]
    if include_uv:
        uv_dir = uv_cache_dir()
        if uv_dir is not None:
            items.append(
                StorageItem(
                    "uv_cache",
                    "uv package cache",
                    str(uv_dir),
                    size_of(uv_dir),
                    True,
                    "Shared by every uv project; pruning removes only unused packages.",
                )
            )
    return items


@dataclass(frozen=True)
class CleanResult:
    key: str
    freed: int
    detail: str


def _remove(paths: Iterable[Path]) -> int:
    freed = 0
    for path in paths:
        size = size_of(path)
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        freed += size - size_of(path)
    return freed


def clean(
    settings: Settings, keys: Iterable[str] = ("tmp", "logs", "database", "ytdlp_cache")
) -> list[CleanResult]:
    """Free space. Never touches music, settings or download history records.

    Don't clean "tmp" while downloads are running (the caller checks)."""
    results: list[CleanResult] = []
    for key in keys:
        if key == "tmp":
            freed = _remove(p for p in tmp_dirs(settings) if p.exists())
            results.append(CleanResult(key, freed, "removed unfinished downloads"))
        elif key == "logs":
            results.append(CleanResult(key, _remove(log_backups()), "removed old log files"))
        elif key == "database":
            db = settings.resolved_archive_path
            before = sum(size_of(db.with_name(db.name + s)) for s in ("", "-wal", "-shm"))
            detail = "compacted the database"
            if db.exists():
                try:
                    conn = sqlite3.connect(db, timeout=30, isolation_level=None)
                    try:
                        conn.execute("VACUUM")
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    finally:
                        conn.close()
                except sqlite3.Error as exc:
                    detail = f"couldn't compact the database right now ({exc})"
            after = sum(size_of(db.with_name(db.name + s)) for s in ("", "-wal", "-shm"))
            results.append(CleanResult(key, max(0, before - after), detail))
        elif key == "ytdlp_cache":
            folder = ytdlp_cache_dir()
            results.append(
                CleanResult(key, _remove([folder] if folder.exists() else []), "cleared")
            )
        elif key == "uv_cache":
            results.append(prune_uv_cache())
        else:
            raise ValueError(f"Unknown item: {key}")
    return results


def prune_uv_cache() -> CleanResult:
    uv_dir = uv_cache_dir()
    uv = shutil.which("uv")
    if uv_dir is None or uv is None:
        return CleanResult("uv_cache", 0, "uv not found")
    before = size_of(uv_dir)
    try:
        proc = subprocess.run(
            [uv, "cache", "prune"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=UV_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CleanResult("uv_cache", 0, f"uv cache prune failed: {exc}")
    if proc.returncode != 0:
        return CleanResult("uv_cache", 0, f"uv cache prune failed: {proc.stderr.strip()[-300:]}")
    return CleanResult("uv_cache", max(0, before - size_of(uv_dir)), "pruned unused packages")
