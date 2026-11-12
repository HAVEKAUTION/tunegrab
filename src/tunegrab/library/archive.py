"""SQLite archive: downloaded tracks, match cache, failures, playlist membership."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tunegrab.models import Candidate, Collection, Track

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS job_history (
    id TEXT PRIMARY KEY, finished_at TEXT NOT NULL, summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS downloads (
    key TEXT PRIMARY KEY,
    spotify_id TEXT,
    path TEXT NOT NULL,
    video_id TEXT,
    format TEXT,
    title TEXT,
    artist TEXT,
    album TEXT,
    track_json TEXT NOT NULL,
    downloaded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS match_cache (
    key TEXT PRIMARY KEY,
    candidate_json TEXT NOT NULL,
    score REAL NOT NULL,
    reason TEXT NOT NULL,
    manual INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    job_input TEXT NOT NULL,
    collection_kind TEXT,
    collection_id TEXT,
    collection_name TEXT,
    position INTEGER,
    track_json TEXT NOT NULL,
    stage TEXT,
    reason TEXT NOT NULL,
    failed_at TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS failures_open ON failures (resolved, key);
CREATE TABLE IF NOT EXISTS playlists (
    playlist_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    synced_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id TEXT NOT NULL,
    key TEXT NOT NULL,
    position INTEGER NOT NULL,
    track_json TEXT NOT NULL,
    PRIMARY KEY (playlist_id, key)
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class DownloadRecord:
    key: str
    path: Path
    video_id: str | None
    format: str | None
    track: Track
    downloaded_at: str

    @property
    def exists(self) -> bool:
        return self.path.is_file()


@dataclass(frozen=True)
class FailureRecord:
    id: int
    key: str
    job_input: str
    collection_kind: str | None
    collection_id: str | None
    collection_name: str | None
    position: int | None
    track: Track
    stage: str | None
    reason: str
    failed_at: str


class Archive:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            if str(path) != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Archive:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock, self._conn:
            return list(self._conn.execute(sql, params).fetchall())

    # --- meta -----------------------------------------------------------------------
    def get_meta(self, key: str) -> Any:
        rows = self._execute("SELECT value FROM meta WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else None

    def set_meta(self, key: str, value: Any) -> None:
        if value is None:
            self._execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            self._execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, dumps(value)),
            )

    # --- job history (web UI) ------------------------------------------------------
    JOB_HISTORY_LIMIT = 200

    def save_job(self, summary: dict[str, Any]) -> None:
        """Remember a finished web job's summary; only the newest 200 are kept."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO job_history (id, finished_at, summary) VALUES (?, ?, ?)",
                (summary["id"], summary.get("finished_at") or _now(), dumps(summary)),
            )
            self._conn.execute(
                "DELETE FROM job_history WHERE id NOT IN "
                "(SELECT id FROM job_history ORDER BY finished_at DESC LIMIT ?)",
                (self.JOB_HISTORY_LIMIT,),
            )

    def job_history(self) -> list[dict[str, Any]]:
        rows = self._execute("SELECT summary FROM job_history ORDER BY finished_at DESC")
        return [json.loads(r["summary"]) for r in rows]

    def clear_job_history(self) -> int:
        with self._lock, self._conn:
            return self._conn.execute("DELETE FROM job_history").rowcount

    # --- downloads -------------------------------------------------------------------
    def get_download(self, key: str) -> DownloadRecord | None:
        rows = self._execute("SELECT * FROM downloads WHERE key = ?", (key,))
        return self._download_row(rows[0]) if rows else None

    def downloaded_path(self, key: str) -> Path | None:
        """Path of an archived download whose file still exists."""
        record = self.get_download(key)
        return record.path if record and record.exists else None

    def record_download(
        self, track: Track, path: Path, video_id: str | None, fmt: str | None
    ) -> None:
        self._execute(
            """INSERT INTO downloads (key, spotify_id, path, video_id, format, title, artist,
                                      album, track_json, downloaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 spotify_id=excluded.spotify_id, path=excluded.path,
                 video_id=excluded.video_id, format=excluded.format, title=excluded.title,
                 artist=excluded.artist, album=excluded.album,
                 track_json=excluded.track_json, downloaded_at=excluded.downloaded_at""",
            (
                track.key,
                track.spotify_id,
                str(path),
                video_id,
                fmt,
                track.title,
                track.primary_artist,
                track.album,
                track.model_dump_json(),
                _now(),
            ),
        )

    def forget_download(self, key: str) -> None:
        self._execute("DELETE FROM downloads WHERE key = ?", (key,))

    def all_downloads(self) -> list[DownloadRecord]:
        rows = self._execute("SELECT * FROM downloads ORDER BY artist, album, title")
        return [self._download_row(r) for r in rows]

    @staticmethod
    def _download_row(row: sqlite3.Row) -> DownloadRecord:
        return DownloadRecord(
            key=row["key"],
            path=Path(row["path"]),
            video_id=row["video_id"],
            format=row["format"],
            track=Track.model_validate_json(row["track_json"]),
            downloaded_at=row["downloaded_at"],
        )

    # --- match cache -----------------------------------------------------------------
    def get_match(self, key: str) -> tuple[Candidate, float, str] | None:
        rows = self._execute(
            "SELECT candidate_json, score, reason FROM match_cache WHERE key = ?", (key,)
        )
        if not rows:
            return None
        row = rows[0]
        return Candidate.model_validate_json(row["candidate_json"]), row["score"], row["reason"]

    def put_match(
        self, key: str, candidate: Candidate, score: float, reason: str, manual: bool = False
    ) -> None:
        """Cache a match. Automatic matches never overwrite a manual override."""
        self._execute(
            """INSERT INTO match_cache (key, candidate_json, score, reason, manual, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 candidate_json=excluded.candidate_json, score=excluded.score,
                 reason=excluded.reason, manual=excluded.manual,
                 updated_at=excluded.updated_at
               WHERE match_cache.manual = 0 OR excluded.manual = 1""",
            (key, candidate.model_dump_json(), score, reason, int(manual), _now()),
        )

    def clear_match(self, key: str) -> None:
        self._execute("DELETE FROM match_cache WHERE key = ?", (key,))

    def is_manual_match(self, key: str) -> bool:
        rows = self._execute("SELECT manual FROM match_cache WHERE key = ?", (key,))
        return bool(rows and rows[0]["manual"])

    # --- failures --------------------------------------------------------------------
    def record_failure(
        self,
        job_input: str,
        collection: Collection | None,
        position: int | None,
        track: Track,
        stage: str | None,
        reason: str,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM failures WHERE key = ? AND resolved = 0", (track.key,))
            self._conn.execute(
                """INSERT INTO failures (key, job_input, collection_kind, collection_id,
                     collection_name, position, track_json, stage, reason, failed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    track.key,
                    job_input,
                    collection.kind if collection else None,
                    collection.id if collection else None,
                    collection.name if collection else None,
                    position,
                    track.model_dump_json(),
                    stage,
                    reason,
                    _now(),
                ),
            )

    def open_failures(self) -> list[FailureRecord]:
        rows = self._execute("SELECT * FROM failures WHERE resolved = 0 ORDER BY id")
        return [
            FailureRecord(
                id=r["id"],
                key=r["key"],
                job_input=r["job_input"],
                collection_kind=r["collection_kind"],
                collection_id=r["collection_id"],
                collection_name=r["collection_name"],
                position=r["position"],
                track=Track.model_validate_json(r["track_json"]),
                stage=r["stage"],
                reason=r["reason"],
                failed_at=r["failed_at"],
            )
            for r in rows
        ]

    def resolve_failures(self, key: str) -> None:
        self._execute("UPDATE failures SET resolved = 1 WHERE key = ? AND resolved = 0", (key,))

    def clear_failures(self) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute("UPDATE failures SET resolved = 1 WHERE resolved = 0")
            return cursor.rowcount

    # --- playlists -------------------------------------------------------------------
    def set_playlist(self, collection: Collection) -> None:
        """Remember a playlist's current members (in order)."""
        if not collection.id:
            return
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO playlists (playlist_id, name, kind, synced_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(playlist_id) DO UPDATE SET name=excluded.name,
                     kind=excluded.kind, synced_at=excluded.synced_at""",
                (collection.id, collection.name, collection.kind, _now()),
            )
            self._conn.execute(
                "DELETE FROM playlist_tracks WHERE playlist_id = ?", (collection.id,)
            )
            seen: set[str] = set()
            for position, track in enumerate(collection.tracks, start=1):
                if track.key in seen:
                    continue
                seen.add(track.key)
                self._conn.execute(
                    """INSERT INTO playlist_tracks (playlist_id, key, position, track_json)
                       VALUES (?, ?, ?, ?)""",
                    (collection.id, track.key, position, track.model_dump_json()),
                )

    def playlist_members(self, playlist_id: str) -> list[tuple[int, Track]]:
        rows = self._execute(
            "SELECT position, track_json FROM playlist_tracks WHERE playlist_id = ? "
            "ORDER BY position",
            (playlist_id,),
        )
        return [(r["position"], Track.model_validate_json(r["track_json"])) for r in rows]

    def playlist_name(self, playlist_id: str) -> str | None:
        rows = self._execute("SELECT name FROM playlists WHERE playlist_id = ?", (playlist_id,))
        return rows[0]["name"] if rows else None

    def playlists(self) -> list[dict[str, Any]]:
        rows = self._execute(
            """SELECT p.playlist_id, p.name, p.kind, p.synced_at, COUNT(t.key) AS tracks
               FROM playlists p LEFT JOIN playlist_tracks t USING (playlist_id)
               GROUP BY p.playlist_id ORDER BY p.name"""
        )
        return [dict(r) for r in rows]

    def keys_in_other_playlists(self, playlist_id: str) -> set[str]:
        rows = self._execute(
            "SELECT DISTINCT key FROM playlist_tracks WHERE playlist_id != ?", (playlist_id,)
        )
        return {r["key"] for r in rows}

    def stats(self) -> dict[str, int]:
        def count(sql: str) -> int:
            return int(self._execute(sql)[0][0])

        return {
            "downloads": count("SELECT COUNT(*) FROM downloads"),
            "cached_matches": count("SELECT COUNT(*) FROM match_cache"),
            "open_failures": count("SELECT COUNT(*) FROM failures WHERE resolved = 0"),
            "playlists": count("SELECT COUNT(*) FROM playlists"),
        }


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
