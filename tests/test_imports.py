"""CSV song lists (Exportify exports and simple hand-made lists)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.fakes import FakeServices
from tunegrab.imports import (
    CsvImportError,
    collection_from_csv,
    playlist_id,
    read_rows,
    spotify_track_id,
)
from tunegrab.metadata.base import NotFoundError
from tunegrab.models import Track

ID1, ID2, ID3 = "4uLU6hMCjMI75M1A2tKUQC", "7ouMYWpwJ422jRcDASZB7P", "0VjIjW4GlUZAMYd2vXMi3b"

EXPORTIFY = (
    "\ufeff"
    '"Track URI","Track Name","Artist URI(s)","Artist Name(s)","Album URI","Album Name",'
    '"Album Artist URI(s)","Album Artist Name(s)","Album Release Date","Album Image URL",'
    '"Disc Number","Track Number","Track Duration (ms)","Track Preview URL","Explicit",'
    '"Popularity","ISRC","Added By","Added At"\n'
    f'"spotify:track:{ID1}","Never Gonna Give You Up","spotify:artist:x","Rick Astley",'
    '"spotify:album:y","Whenever You Need Somebody","spotify:artist:x","Rick Astley",'
    '"1987-11-12","https://i.scdn.co/image/abc","1","1","213573","","false","80",'
    '"gbarl9300135","",""\n'
    f'"spotify:track:{ID2}","Get Lucky (feat. Pharrell Williams)","a,b",'
    '"Daft Punk,Pharrell Williams,Nile Rodgers","z","Random Access Memories","a","Daft Punk",'
    '"2013","","1","8","369626","","true","85","USQX91300108","",""\n'
)


def test_exportify_rows_become_tracks_without_lookups() -> None:
    rows = read_rows(EXPORTIFY)
    assert all(isinstance(r, Track) for r in rows)
    rick, lucky = rows
    assert isinstance(rick, Track) and isinstance(lucky, Track)
    assert rick.spotify_id == ID1 and rick.title == "Never Gonna Give You Up"
    assert rick.artists == ["Rick Astley"] and rick.album == "Whenever You Need Somebody"
    assert rick.release_date == "1987-11-12" and rick.duration_ms == 213573
    assert rick.isrc == "GBARL9300135" and rick.track_number == 1 and rick.disc_number == 1
    assert rick.cover_url == "https://i.scdn.co/image/abc" and rick.explicit is False
    assert lucky.artists == ["Daft Punk", "Pharrell Williams", "Nile Rodgers"]
    assert lucky.album_artist == "Daft Punk" and lucky.year == "2013" and lucky.explicit


def test_simple_semicolon_list_and_blank_rows() -> None:
    text = "Title;Artist\nOne More Time;Daft Punk\n\n;\nBreathe;Télépopmusik; Angela McCluskey\n"
    rows = read_rows(text)
    assert [r.title for r in rows if isinstance(r, Track)] == ["One More Time", "Breathe"]


def test_uri_only_rows_are_looked_up_in_order() -> None:
    text = f"Track URI\nspotify:track:{ID1}\nhttps://open.spotify.com/track/{ID2}?si=x\n{ID3}\n"
    assert read_rows(text) == [ID1, ID2, ID3]

    def lookup(track_id: str) -> Track:
        if track_id == ID2:
            raise NotFoundError("gone")
        return Track(spotify_id=track_id, title=f"Song {track_id[:3]}", artists=["A"])

    collection = collection_from_csv(text, "My List", lookup=lookup)
    assert [t.spotify_id for t in collection.tracks] == [ID1, ID3]
    assert collection.kind == "playlist" and collection.id == "csv:my-list"
    assert collection.skipped == 1 and collection.total == 3 and collection.warnings


def test_bad_csv_files() -> None:
    with pytest.raises(CsvImportError, match="empty"):
        read_rows("")
    with pytest.raises(CsvImportError, match="Track Name"):
        read_rows("foo,bar\n1,2\n")
    with pytest.raises(CsvImportError, match="No songs"):
        collection_from_csv("Track URI\nnot-an-id\n", "x")


def test_helpers() -> None:
    assert spotify_track_id(f"spotify:track:{ID1}") == ID1
    assert spotify_track_id("hello") is None
    assert playlist_id("Liked Songs!") == "csv:liked-songs"
    assert playlist_id("***") == "csv:songs"


def test_cli_download_from_exportify_csv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tunegrab import cli

    monkeypatch.setattr(cli, "build_services", lambda settings, http=None: FakeServices(settings))
    monkeypatch.setenv("TUNEGRAB_OUTPUT_DIR", str(tmp_path / "lib"))
    monkeypatch.setenv("TUNEGRAB_LYRICS", "false")
    csv_file = tmp_path / "Liked_Songs.csv"
    csv_file.write_text(EXPORTIFY, "utf-8")
    result = CliRunner().invoke(cli.app, ["download", "--from-file", str(csv_file)])
    assert result.exit_code == 0, result.output
    lib = tmp_path / "lib"
    audio = [f for f in lib.rglob("*.mp3") if ".tunegrab-tmp" not in f.parts]
    assert len(audio) == 2
    playlist = lib / "Playlists" / "Liked Songs.m3u8"
    assert playlist.exists()
    assert "Never Gonna Give You Up" in playlist.read_text("utf-8")
