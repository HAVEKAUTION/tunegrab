from pathlib import Path

import pytest

from tests.fakes import candidate_for, make_track
from tunegrab.config import DEFAULT_PLAYLIST_TEMPLATE, DEFAULT_TRACK_TEMPLATE
from tunegrab.library.archive import Archive
from tunegrab.library.paths import (
    MAX_PATH_CHARS,
    TemplateError,
    render_path,
    sanitize_segment,
    template_fields,
    unique_path,
    validate_template,
)
from tunegrab.library.playlists import playlist_file, read_m3u8, write_m3u8
from tunegrab.models import Collection

# --- paths ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AC/DC", "AC-DC"),
        ("What? Why*", "What Why"),
        ('Say "hi"', "Say 'hi'"),
        ("Re: Stacks", "Re - Stacks"),
        ("a<b>c|d\\e", "abc-d-e"),
        ("trailing dots...", "trailing dots"),
        ("...hidden", "hidden"),
        ("CON", "_CON"),
        ("con.mp3", "_con.mp3"),
        ("LPT1", "_LPT1"),
        ("Console", "Console"),
        ("", "_"),
        ("tab\there\x00", "tab here"),
        ("Café", "Café"),
    ],
)
def test_sanitize(raw: str, expected: str) -> None:
    assert sanitize_segment(raw) == expected


def test_sanitize_length_limits() -> None:
    assert len(sanitize_segment("x" * 500)) == 120
    assert len(sanitize_segment("界" * 200).encode()) <= 240


def test_default_template(tmp_path: Path) -> None:
    track = make_track(
        3,
        album="Discovery",
        album_artist="Daft Punk",
        artists=["Daft Punk"],
        title="Digital Love",
        release_date="2001-03-12",
    )
    path = render_path(DEFAULT_TRACK_TEMPLATE, template_fields(track, "mp3"), tmp_path)
    assert path == tmp_path / "Daft Punk" / "Discovery (2001)" / "03 - Digital Love.mp3"


def test_missing_fields_are_tidied(tmp_path: Path) -> None:
    track = make_track(1, album=None, album_artist=None, track_number=None, release_date=None)
    path = render_path(DEFAULT_TRACK_TEMPLATE, template_fields(track, "m4a"), tmp_path)
    assert path == tmp_path / "Artist 1" / "Unknown Album" / "Song 1.m4a"


def test_playlist_template(tmp_path: Path) -> None:
    fields = template_fields(make_track(2), "opus", playlist="Road: Trip", position=7)
    path = render_path(DEFAULT_PLAYLIST_TEMPLATE, fields, tmp_path)
    assert path == tmp_path / "Playlists" / "Road - Trip" / "007 - Artist 2 - Song 2.opus"


def test_values_with_slashes_stay_in_one_segment(tmp_path: Path) -> None:
    track = make_track(1, album_artist="AC/DC", title="A/B")
    path = render_path(DEFAULT_TRACK_TEMPLATE, template_fields(track, "mp3"), tmp_path)
    assert path.relative_to(tmp_path).parts[0] == "AC-DC"
    assert path.name == "01 - A-B.mp3"


def test_long_paths_shrink_folders_before_title(tmp_path: Path) -> None:
    root = tmp_path / ("r" * 100)
    track = make_track(2, album="A" * 150, title="Endpoints")
    path = render_path(DEFAULT_TRACK_TEMPLATE, template_fields(track, "mp3"), root)
    assert len(str(path)) <= MAX_PATH_CHARS
    assert path.name == "02 - Endpoints.mp3"


def test_validate_template() -> None:
    validate_template(DEFAULT_TRACK_TEMPLATE)
    with pytest.raises(TemplateError, match="Unknown"):
        validate_template("{nope}.{ext}")
    with pytest.raises(TemplateError, match="ext"):
        validate_template("{title}.mp3")


def test_unique_path(tmp_path: Path) -> None:
    p = tmp_path / "a.mp3"
    assert unique_path(p) == p
    p.write_text("x")
    assert unique_path(p) == tmp_path / "a (2).mp3"
    (tmp_path / "a (2).mp3").write_text("x")
    assert unique_path(p) == tmp_path / "a (3).mp3"


# --- archive --------------------------------------------------------------------


def test_downloads(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "db" / "a.db")
    track = make_track(1)
    file = tmp_path / "x.mp3"
    assert archive.downloaded_path(track.key) is None
    archive.record_download(track, file, "vid", "mp3")
    assert archive.downloaded_path(track.key) is None  # file doesn't exist yet
    file.write_bytes(b"x")
    assert archive.downloaded_path(track.key) == file
    record = archive.get_download(track.key)
    assert record and record.track == track and record.video_id == "vid"
    archive.record_download(track, file, "vid2", "mp3")  # upsert
    assert len(archive.all_downloads()) == 1
    archive.forget_download(track.key)
    assert archive.get_download(track.key) is None


def test_manual_match_not_overwritten(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "a.db")
    track = make_track(1)
    manual = candidate_for(track, video_id="manual00000")
    archive.put_match(track.key, manual, 100, "manual", manual=True)
    archive.put_match(track.key, candidate_for(track, video_id="auto0000000"), 95, "auto")
    hit = archive.get_match(track.key)
    assert hit and hit[0].video_id == "manual00000" and archive.is_manual_match(track.key)
    archive.put_match(track.key, candidate_for(track, video_id="manual11111"), 100, "m", True)
    assert archive.get_match(track.key)[0].video_id == "manual11111"  # type: ignore[index]
    archive.clear_match(track.key)
    assert archive.get_match(track.key) is None


def test_failures(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "a.db")
    coll = Collection(kind="playlist", id="p1", name="PL")
    t1, t2 = make_track(1), make_track(2)
    archive.record_failure("url", coll, 1, t1, "matching", "no match")
    archive.record_failure("url", coll, 1, t1, "downloading", "again")  # replaces the open one
    archive.record_failure("url", None, None, t2, None, "x")
    failures = archive.open_failures()
    assert [(f.key, f.reason) for f in failures] == [(t1.key, "again"), (t2.key, "x")]
    assert failures[0].collection_name == "PL" and failures[0].track == t1
    archive.resolve_failures(t1.key)
    assert [f.key for f in archive.open_failures()] == [t2.key]
    assert archive.clear_failures() == 1 and archive.open_failures() == []


def test_playlists(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "a.db")
    tracks = [make_track(1), make_track(2), make_track(1)]  # duplicate entry
    archive.set_playlist(Collection(kind="playlist", id="p1", name="Mine", tracks=tracks))
    archive.set_playlist(Collection(kind="playlist", id="p2", name="Other", tracks=[make_track(2)]))
    members = archive.playlist_members("p1")
    assert [(pos, t.title) for pos, t in members] == [(1, "Song 1"), (2, "Song 2")]
    assert archive.playlist_name("p1") == "Mine" and archive.playlist_name("zz") is None
    assert archive.keys_in_other_playlists("p1") == {make_track(2).key}
    archive.set_playlist(
        Collection(kind="playlist", id="p1", name="Mine v2", tracks=[make_track(3)])
    )
    assert [t.title for _, t in archive.playlist_members("p1")] == ["Song 3"]
    stats = archive.stats()
    assert stats["playlists"] == 2 and stats["downloads"] == 0
    assert {p["name"] for p in archive.playlists()} == {"Mine v2", "Other"}


# --- m3u ------------------------------------------------------------------------


def test_m3u8_relative_paths(tmp_path: Path) -> None:
    song = tmp_path / "Artist" / "Album" / "01 - Song.mp3"
    song.parent.mkdir(parents=True)
    song.write_bytes(b"x")
    target = playlist_file(tmp_path, "My: List")
    assert target == tmp_path / "Playlists" / "My - List.m3u8"
    write_m3u8(target, "My: List", [(song, make_track(1, duration_ms=201_400))])
    text = target.read_text("utf-8")
    assert text.splitlines() == [
        "#EXTM3U",
        "#PLAYLIST:My: List",
        "#EXTINF:201,Artist 1 - Song 1",
        "../Artist/Album/01 - Song.mp3",
    ]
    assert read_m3u8(target) == [song.resolve()]


@pytest.mark.parametrize(
    "template",
    [
        "{title.__class__}.{ext}",  # attribute lookups (format-string tricks)
        "{title[0]}.{ext}",  # index lookups
        "{title}.{ext}.bat",  # must end in the audio extension
        "{ext}/{title}",
    ],
)
def test_validate_template_rejects_tricks(template: str) -> None:
    with pytest.raises(TemplateError):
        validate_template(template)


def test_render_path_blocks_lookups_and_traversal(tmp_path: Path) -> None:
    track = make_track(1)
    with pytest.raises(TemplateError):
        render_path("{title.__class__}.{ext}", template_fields(track, "mp3"), tmp_path)
    path = render_path("../../{title}.{ext}", template_fields(track, "mp3"), tmp_path)
    assert tmp_path in path.parents


def test_move_into_place_never_overwrites(tmp_path: Path) -> None:
    import threading

    from tunegrab.library.paths import move_into_place

    target = tmp_path / "Artist" / "Album" / "Intro.mp3"
    sources = []
    for i in range(8):
        src = tmp_path / f"tmp{i}.mp3"
        src.write_bytes(f"song {i}".encode())
        sources.append(src)
    results: list[Path] = []
    barrier = threading.Barrier(len(sources))

    def finish(src: Path) -> None:
        barrier.wait()  # all eight "finish downloading" at the same moment
        results.append(move_into_place(src, target))

    threads = [threading.Thread(target=finish, args=(s,)) for s in sources]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(results)) == 8
    assert sorted(p.read_bytes() for p in results) == sorted(f"song {i}".encode() for i in range(8))
    assert not any(s.exists() for s in sources)


def test_move_into_place_keeps_existing_files_and_cleans_up_on_failure(tmp_path: Path) -> None:
    from tunegrab.library.paths import move_into_place

    target = tmp_path / "a.mp3"
    target.write_bytes(b"user's own file")
    src = tmp_path / "new.mp3"
    src.write_bytes(b"new")
    assert move_into_place(src, target) == tmp_path / "a (2).mp3"
    assert target.read_bytes() == b"user's own file"
    with pytest.raises(OSError):
        move_into_place(tmp_path / "missing.mp3", target)
    assert not (tmp_path / "a (3).mp3").exists()  # the placeholder was removed


def test_corrupt_database_is_set_aside_and_replaced(tmp_path: Path) -> None:
    from tunegrab.library import archive as archive_module

    archive_module.startup_warnings.clear()
    db = tmp_path / "tunegrab.db"
    db.write_bytes(b"this is not a database" * 100)
    with Archive(db) as archive:
        archive.record_download(make_track(1), tmp_path / "a.mp3", "vid", "mp3")
        assert archive.stats()["downloads"] == 1
    kept = list(tmp_path.glob("tunegrab.db.corrupt-*"))
    assert len(kept) == 1 and kept[0].read_bytes().startswith(b"this is not")
    assert len(archive_module.startup_warnings) == 1
    assert "music files are untouched" in archive_module.startup_warnings[0]
    archive_module.startup_warnings.clear()


def test_one_unreadable_row_does_not_break_the_library(tmp_path: Path) -> None:
    with Archive(tmp_path / "t.db") as archive:
        archive.record_download(make_track(1), tmp_path / "a.mp3", "v1", "mp3")
        archive.record_download(make_track(2), tmp_path / "b.mp3", "v2", "mp3")
        archive._execute(
            "UPDATE downloads SET track_json = '{\"title\": 5}' WHERE key = ?",
            (make_track(1).key,),
        )
        assert [r.track.title for r in archive.all_downloads()] == ["Song 2"]
        assert archive.get_download(make_track(1).key) is None
