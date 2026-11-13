"""Playlist sync: which songs left a playlist since it was last downloaded, and removing
their files once the user has confirmed it. Shared by ``tunegrab sync`` and Library.

Songs that left are remembered (archive meta ``sync_gone:<playlist>``) until their files
are removed or the user chooses to keep them, so nothing has to be decided on the spot.
"""

from __future__ import annotations

import logging
from collections.abc import Collection as Keys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from tunegrab.library.archive import Archive
from tunegrab.models import Collection, Track

log = logging.getLogger(__name__)

# "file": its file can be removed; "shared": kept because another playlist has the song;
# "not_downloaded": there is no file (never downloaded, or already moved or deleted).
GoneState = Literal["file", "shared", "not_downloaded"]


@dataclass(frozen=True)
class Gone:
    """A song that is no longer in a playlist."""

    track: Track
    state: GoneState
    path: Path | None = None


@dataclass
class Removal:
    deleted: list[Gone] = field(default_factory=list)
    failed: list[tuple[Gone, str]] = field(default_factory=list)  # (song, why not)


def _pending_key(playlist_id: str) -> str:
    return f"sync_gone:{playlist_id}"


def pending(archive: Archive, playlist_id: str) -> list[Track]:
    """Songs that left the playlist earlier and haven't been dealt with yet."""
    found: list[Track] = []
    for raw in archive.get_meta(_pending_key(playlist_id)) or []:
        try:
            found.append(Track.model_validate(raw))
        except ValueError as exc:
            log.warning("skipping an unreadable departed song of %s: %s", playlist_id, exc)
    return found


def _save_pending(archive: Archive, playlist_id: str, tracks: list[Track]) -> None:
    archive.set_meta(_pending_key(playlist_id), [t.model_dump(mode="json") for t in tracks] or None)


def remembered(archive: Archive, playlist_id: str) -> dict[str, Track]:
    """The playlist as TuneGrab last saw it, plus songs that left it earlier and are still
    waiting for a decision. Read this *before* the playlist is downloaded again."""
    before = {track.key: track for _, track in archive.playlist_members(playlist_id)}
    for track in pending(archive, playlist_id):
        before.setdefault(track.key, track)
    return before


def is_complete(collection: Collection) -> bool:
    """False when the source listed only part of the playlist (without a Spotify login
    only the first 100 songs can be read). A missing song then proves nothing, so no song
    may be treated as having left."""
    if collection.total is None:
        return True
    return len(collection.tracks) + collection.skipped >= collection.total


def note_departures(
    archive: Archive, playlist_id: str, before: dict[str, Track], collection: Collection
) -> list[Track]:
    """Work out which of the ``before`` songs are no longer in ``collection``, remember
    them, and return them. An incomplete listing changes nothing and returns none."""
    if not is_complete(collection):
        return []
    current = {track.key for track in collection.tracks}
    gone = [track for key, track in before.items() if key not in current]
    _save_pending(archive, playlist_id, gone)
    return gone


def describe(archive: Archive, playlist_id: str, gone: list[Track]) -> list[Gone]:
    """What would happen to each departed song's file."""
    shared = archive.keys_in_other_playlists(playlist_id)
    listed: list[Gone] = []
    for track in gone:
        record = archive.get_download(track.key)
        if record is None or not record.exists:
            listed.append(Gone(track, "not_downloaded"))
        elif track.key in shared:
            listed.append(Gone(track, "shared", record.path))
        else:
            listed.append(Gone(track, "file", record.path))
    return listed


def remove_files(archive: Archive, playlist_id: str, keys: Keys[str] | None = None) -> Removal:
    """Delete the files (and .lrc lyrics) of songs that left the playlist: all of them, or
    only ``keys``. Songs another playlist still has are never touched. The caller must
    have the user's confirmation; a file that can't be deleted stays pending."""
    gone = pending(archive, playlist_id)
    result = Removal()
    for item in describe(archive, playlist_id, gone):
        if item.state != "file" or item.path is None:
            continue
        if keys is not None and item.track.key not in keys:
            continue
        try:
            item.path.unlink(missing_ok=True)
            item.path.with_suffix(".lrc").unlink(missing_ok=True)
        except OSError as exc:
            result.failed.append((item, exc.strerror or str(exc)))
            continue
        archive.forget_download(item.track.key)
        result.deleted.append(item)
    done = {item.track.key for item in result.deleted}
    _save_pending(archive, playlist_id, [t for t in gone if t.key not in done])
    return result


def keep_files(archive: Archive, playlist_id: str) -> None:
    """The user wants to keep the songs that left: stop listing them."""
    _save_pending(archive, playlist_id, [])
