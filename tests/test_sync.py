"""Playlist sync logic shared by the CLI and the app (tunegrab.library.sync)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fakes import make_track
from tunegrab.library import sync
from tunegrab.library.archive import Archive
from tunegrab.models import Collection, Track


def playlist(playlist_id: str, tracks: list[Track], **extra: object) -> Collection:
    return Collection(kind="playlist", id=playlist_id, name=playlist_id, tracks=tracks, **extra)  # type: ignore[arg-type]


def saved(archive: Archive, folder: Path, track: Track) -> Path:
    path = folder / f"{track.title}.mp3"
    path.write_bytes(b"audio")
    path.with_suffix(".lrc").write_text("[00:01.00] la", encoding="utf-8")
    archive.record_download(track, path, "vid", "mp3")
    return path


@pytest.fixture
def archive(tmp_path: Path) -> Archive:
    return Archive(tmp_path / "a.db")


def test_departures_are_remembered_until_dealt_with(archive: Archive, tmp_path: Path) -> None:
    one, two, three, four = (make_track(n) for n in (1, 2, 3, 4))
    archive.set_playlist(playlist("mix", [one, two, three]))
    paths = {t.key: saved(archive, tmp_path, t) for t in (one, two)}
    before = sync.remembered(archive, "mix")
    assert list(before) == [one.key, two.key, three.key]

    now = playlist("mix", [one, four])
    gone = sync.note_departures(archive, "mix", before, now)
    assert [t.key for t in gone] == [two.key, three.key]
    archive.set_playlist(now)  # what a download of the playlist does
    # still known on the next sync, although the playlist table has moved on
    assert set(sync.remembered(archive, "mix")) == {one.key, four.key, two.key, three.key}
    listed = sync.describe(archive, "mix", sync.pending(archive, "mix"))
    assert [(g.track.key, g.state, g.path) for g in listed] == [
        (two.key, "file", paths[two.key]),
        (three.key, "not_downloaded", None),
    ]
    assert all(p.exists() for p in paths.values())  # listing never deletes

    # a song that comes back is no longer pending
    back = playlist("mix", [one, two, four])
    again = sync.note_departures(archive, "mix", sync.remembered(archive, "mix"), back)
    assert [t.key for t in again] == [three.key]


def test_remove_files_deletes_only_what_was_named(archive: Archive, tmp_path: Path) -> None:
    one, two, three = (make_track(n) for n in (1, 2, 3))
    archive.set_playlist(playlist("mix", [one, two, three]))
    paths = {t.key: saved(archive, tmp_path, t) for t in (one, two, three)}
    sync.note_departures(archive, "mix", sync.remembered(archive, "mix"), playlist("mix", [one]))

    result = sync.remove_files(archive, "mix", {two.key, one.key, "unknown"})
    assert [g.track.key for g in result.deleted] == [two.key] and result.failed == []
    assert not paths[two.key].exists() and not paths[two.key].with_suffix(".lrc").exists()
    assert archive.get_download(two.key) is None
    assert paths[one.key].exists()  # still in the playlist: naming it changes nothing
    assert paths[three.key].exists() and [t.key for t in sync.pending(archive, "mix")] == [
        three.key
    ]

    sync.keep_files(archive, "mix")
    assert sync.pending(archive, "mix") == [] and paths[three.key].exists()
    assert sync.remove_files(archive, "mix").deleted == []  # nothing pending, nothing deleted


def test_songs_in_another_playlist_are_never_deleted(archive: Archive, tmp_path: Path) -> None:
    one, two = make_track(1), make_track(2)
    archive.set_playlist(playlist("mix", [one, two]))
    archive.set_playlist(playlist("other", [two]))
    path = saved(archive, tmp_path, two)
    sync.note_departures(archive, "mix", sync.remembered(archive, "mix"), playlist("mix", [one]))
    [item] = sync.describe(archive, "mix", sync.pending(archive, "mix"))
    assert item.state == "shared"
    result = sync.remove_files(archive, "mix", {two.key})
    assert result.deleted == [] and path.exists() and archive.get_download(two.key) is not None


def test_file_that_cannot_be_deleted_stays_pending(
    archive: Archive, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    one, two = make_track(1), make_track(2)
    archive.set_playlist(playlist("mix", [one, two]))
    path = saved(archive, tmp_path, two)
    sync.note_departures(archive, "mix", sync.remembered(archive, "mix"), playlist("mix", [one]))
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self.suffix == ".mp3":
            raise PermissionError(13, "The file is in use", str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    result = sync.remove_files(archive, "mix")
    assert result.deleted == [] and len(result.failed) == 1
    assert result.failed[0][1] == "The file is in use"
    assert path.exists() and archive.get_download(two.key) is not None
    assert [t.key for t in sync.pending(archive, "mix")] == [two.key]


def test_partial_listing_proves_nothing(archive: Archive) -> None:
    """Without a login only the first 100 songs of a playlist can be read: the rest
    haven't left, and must not be offered for deletion."""
    tracks = [make_track(n) for n in (1, 2, 3)]
    archive.set_playlist(playlist("mix", tracks))
    before = sync.remembered(archive, "mix")
    partial = playlist("mix", tracks[:1], total=3)
    assert not sync.is_complete(partial)
    assert sync.note_departures(archive, "mix", before, partial) == []
    assert sync.pending(archive, "mix") == []
    assert sync.is_complete(playlist("mix", tracks[:2], total=3, skipped=1))  # podcasts etc.
    assert sync.is_complete(playlist("mix", tracks))  # no total known
