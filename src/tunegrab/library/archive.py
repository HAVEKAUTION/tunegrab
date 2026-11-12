"""SQLite archive: downloaded tracks, match cache, failures, playlist membership."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import ValidationError

from tunegrab.config import set_aside, startup_warnings
from tunegrab.errors import TuneGrabError
from tunegrab.models import Candidate, Collection, Track

log = logging.getLogger(__name__)
T = TypeVar("T")

SCHEMA_VERSION = 1
# Version n -> the statements that upgrade a version n-1 history to n, run in one
# transaction after a backup copy is made. SCHEMA below is always the newest layout (it
# creates fresh histories and any table an older one lacks).
MIGRATIONS: dict[int, tuple[str, ...]] = {}
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


class SchemaTooNewError(TuneGrabError):
    """The history was written by a newer TuneGrab; opening it here could damage it."""


class MigrationError(TuneGrabError):
    """Upgrading the history failed; it was left as it was (and a backup exists)."""


class Archive:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._memory = str(path) == ":memory:"
        if not self._memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._conn = self._open()
        except sqlite3.DatabaseError as exc:
            if self._memory:
                raise
            self._conn = self._recover(exc)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                if not self._memory:
                    conn.execute("PRAGMA journal_mode=WAL")
                    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise sqlite3.DatabaseError("integrity check failed")
            self._migrate(conn)
            with conn:
                conn.executescript(SCHEMA)
                conn.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
        except BaseException:
            conn.close()
            raise
        return conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Bring an older history up to SCHEMA_VERSION (backup first, one transaction)."""
        has_meta = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
        ).fetchone()
        if not has_meta:
            return  # a new, empty history
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        try:
            version = int(json.loads(row[0])) if row else 1
        except (TypeError, ValueError) as exc:
            raise sqlite3.DatabaseError(f"bad schema version {row[0]!r}") from exc
        if version > SCHEMA_VERSION:
            raise SchemaTooNewError(
                f"Your download history ({self.path}) was made by a newer TuneGrab "
                f"(format {version}; this version reads up to {SCHEMA_VERSION}). Update "
                "TuneGrab to use it; it hasn't been changed."
            )
        if version == SCHEMA_VERSION:
            return
        backup = self.path.with_name(f"{self.path.name}.bak-v{version}")
        if not self._memory:
            target = sqlite3.connect(str(backup))
            try:
                conn.backup(target)
            finally:
                target.close()
            log.info("backed up the download history to %s before upgrading it", backup)
        conn.execute("BEGIN")
        try:
            for step in range(version + 1, SCHEMA_VERSION + 1):
                for statement in MIGRATIONS[step]:
                    conn.execute(statement)
            conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),)
            )
            if row is None:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
        except sqlite3.Error as exc:
            conn.rollback()
            raise MigrationError(
                f"TuneGrab couldn't upgrade your download history ({exc}). It was left as it "
                f"was; a copy is at {backup}."
            ) from exc
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
        log.info("download history upgraded from format %s to %s", version, SCHEMA_VERSION)

    def _recover(self, exc: sqlite3.DatabaseError) -> sqlite3.Connection:
        """The download history is damaged: keep it aside and start a fresh one."""
        kept = set_aside(self.path, "corrupt")
        for suffix in ("-wal", "-shm"):
            side = self.path.with_name(self.path.name + suffix)
            if side.exists():
                set_aside(side, "corrupt")
        log.error("download history %s was unreadable (%s); kept as %s", self.path, exc, kept)
        startup_warnings.append(
            f"Your download history couldn't be read ({exc}), so TuneGrab started a new one. "
            f"Your music files are untouched; the old history was kept as {kept}. Songs you "
            "already have are recognised again when you download them."
        )
        return self._open()

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
        return _parse_rows(rows, lambda r: dict(json.loads(r["summary"])), "job history")

    def clear_job_history(self) -> int:
        with self._lock, self._conn:
            return self._conn.execute("DELETE FROM job_history").rowcount

    # --- downloads -------------------------------------------------------------------
    def get_download(self, key: str) -> DownloadRecord | None:
        rows = self._execute("SELECT * FROM downloads WHERE key = ?", (key,))
        found = _parse_rows(rows, self._download_row, "download")
        return found[0] if found else None

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

    def path_is_recorded(self, path: Path) -> bool:
        """True if any archived download (of any song) points at ``path``."""
        return bool(self._execute("SELECT 1 FROM downloads WHERE path = ? LIMIT 1", (str(path),)))

    def forget_download(self, key: str) -> None:
        self._execute("DELETE FROM downloads WHERE key = ?", (key,))

    def all_downloads(self) -> list[DownloadRecord]:
        rows = self._execute("SELECT * FROM downloads ORDER BY artist, album, title")
        return _parse_rows(rows, self._download_row, "download")

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
        return _parse_rows(
            rows,
            lambda r: FailureRecord(
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
            ),
            "failure",
        )

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
        return _parse_rows(
            rows, lambda r: (r["position"], Track.model_validate_json(r["track_json"])), "member"
        )

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


def _parse_rows(
    rows: Iterable[sqlite3.Row], parse: Callable[[sqlite3.Row], T], what: str
) -> list[T]:
    """Parse rows, skipping (and logging) any that no longer fit the model, so one bad
    record can't take down the whole Library or retry list."""
    parsed: list[T] = []
    for row in rows:
        try:
            parsed.append(parse(row))
        except (ValidationError, ValueError, TypeError, KeyError) as exc:
            log.warning("skipping an unreadable %s record: %s", what, exc)
    return parsed
