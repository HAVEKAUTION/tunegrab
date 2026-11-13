"""tunegrab.storage: sizes and cleaning (temp folders, logs, database, yt-dlp cache)."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from tunegrab import storage
from tunegrab.config import Settings


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for name in ("CONFIG", "DATA", "LOG"):
        monkeypatch.setenv(f"TUNEGRAB_{name}_DIR", str(tmp_path / name.lower()))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return Settings(
        _env_file=None, output_dir=tmp_path / "lib", archive_path=tmp_path / "data" / "t.db"
    )


def test_report_and_clean(env: Settings, tmp_path: Path) -> None:
    tmp = env.resolved_output_dir / storage.TMP_DIR_NAME / "task-x"
    tmp.mkdir(parents=True)
    (tmp / "part.webm").write_bytes(b"x" * 1000)
    old = time.time() - 2 * storage.IN_USE_S
    for p in (tmp, tmp / "part.webm"):
        os.utime(p, (old, old))
    running = env.resolved_output_dir / storage.TMP_DIR_NAME / "task-now"
    running.mkdir()
    (running / "part.webm").write_bytes(b"")  # a download in progress: must survive
    song = env.resolved_output_dir / "Artist" / "song.mp3"
    song.parent.mkdir(parents=True)
    song.write_bytes(b"music")
    logs = tmp_path / "log"
    logs.mkdir()
    (logs / "tunegrab.log").write_text("current")
    (logs / "tunegrab.log.1").write_bytes(b"y" * 500)
    cache = tmp_path / "cache" / "yt-dlp" / "youtube-sigfuncs"
    cache.mkdir(parents=True)
    (cache / "a.json").write_text("{}")
    db = env.resolved_archive_path
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE t (x BLOB)")
        conn.executemany("INSERT INTO t VALUES (?)", [(b"z" * 4000,)] * 50)
        conn.execute("DELETE FROM t")
    conn.close()

    sizes = {i.key: i.size for i in storage.report(env)}
    assert sizes["tmp"] == 1000 and sizes["logs"] == 500 and sizes["ytdlp_cache"] == 2
    assert storage.database_free_bytes(db) > 100_000

    results = {r.key: r for r in storage.clean(env)}
    assert results["tmp"].freed == 1000 and results["logs"].freed == 500
    assert results["database"].freed > 100_000
    assert not tmp.exists() and (running / "part.webm").exists()
    assert not (logs / "tunegrab.log.1").exists() and (logs / "tunegrab.log").exists()
    assert not (tmp_path / "cache" / "yt-dlp").exists()
    assert song.read_bytes() == b"music"  # music is never touched
    assert sqlite3.connect(db).execute("SELECT count(*) FROM t").fetchone() == (0,)


def test_clean_rejects_unknown(env: Settings) -> None:
    with pytest.raises(ValueError):
        storage.clean(env, ["music"])


def test_human_size() -> None:
    assert storage.human_size(512) == "512 B"
    assert storage.human_size(1536) == "1.5 KB"
    assert storage.human_size(3 * 1024**3) == "3.0 GB"
