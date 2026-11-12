import os
from pathlib import Path

import pytest

from tunegrab.config import (
    Settings,
    coerce_setting,
    config_file,
    keep_earlier_defaults,
    pin_music_folder,
    read_config_file,
    write_config_file,
)


@pytest.fixture(autouse=True)
def real_format_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TUNEGRAB_FORMAT")  # conftest sets MP3 for the rest of the suite


def test_defaults() -> None:
    s = Settings(_env_file=None)
    assert (s.format, s.bitrate, s.threads, s.playlist_mode, s.lyrics) == (
        "m4a",
        None,
        4,
        "m3u",
        True,
    )
    assert s.resolved_archive_path.name == "tunegrab.db"
    assert not s.has_spotify_credentials


def test_precedence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    write_config_file({"format": "opus", "threads": 2, "bitrate": 192})
    assert Settings(_env_file=None).format == "opus"
    (tmp_path / ".env").write_text("TUNEGRAB_THREADS=6\nSPOTIFY_CLIENT_ID=from-dotenv\n")
    s = Settings()
    assert (s.threads, s.bitrate, s.spotify_client_id) == (6, 192, "from-dotenv")
    monkeypatch.setenv("TUNEGRAB_THREADS", "8")
    assert Settings().threads == 8  # env beats .env
    assert Settings(threads=1).threads == 1  # CLI flag beats everything


def test_blank_credentials_are_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "  ")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "")
    s = Settings(_env_file=None)
    assert s.spotify_client_id is None and s.spotify_client_secret is None


def test_credentials_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "id")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "hunter2")
    s = Settings(_env_file=None)
    assert s.has_spotify_credentials and "hunter2" not in repr(s)


def test_invalid_values_rejected() -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, format="wav")
    with pytest.raises(ValueError):
        Settings(_env_file=None, threads=0)


def test_coerce_setting() -> None:
    assert coerce_setting("threads", "8") == 8
    assert coerce_setting("lyrics", "false") is False
    assert coerce_setting("format", "flac") == "flac"
    assert coerce_setting("output_dir", "~/Music") == str(Path("~/Music"))
    assert coerce_setting("archive_path", "none") is None
    assert coerce_setting("spotify_client_secret", "s3") == "s3"
    with pytest.raises(KeyError):
        coerce_setting("nope", "1")
    with pytest.raises(ValueError):
        coerce_setting("threads", "99")
    with pytest.raises(ValueError):
        coerce_setting("format", "wav")


def test_config_file_roundtrip() -> None:
    values = {
        "format": "m4a",
        "threads": 3,
        "lyrics": False,
        "match_threshold": 75.5,
        "track_template": '{artist}/"{title}".{ext}',
        "output_dir": "C:\\Music\\Lib",
    }
    path = write_config_file(values)
    assert path == config_file() and read_config_file() == values
    s = Settings(_env_file=None)
    assert (s.format, s.threads, s.lyrics, s.match_threshold) == ("m4a", 3, False, 75.5)
    assert s.output_dir == Path("C:\\Music\\Lib")


def test_unreadable_config_file_is_set_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tunegrab import config

    monkeypatch.setenv("TUNEGRAB_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config, "startup_warnings", [])
    (tmp_path / "config.toml").write_text('format = "opus\nthreads = = 2\n', encoding="utf-8")
    settings = Settings(_env_file=None)
    assert settings.format == "m4a"  # defaults
    kept = list(tmp_path.glob("config.toml.broken-*"))
    assert len(kept) == 1 and "threads = = 2" in kept[0].read_text(encoding="utf-8")
    assert not (tmp_path / "config.toml").exists()
    assert len(config.startup_warnings) == 1 and str(kept[0]) in config.startup_warnings[0]


# --- music folder default (C1) ------------------------------------------------------------
def test_fresh_profile_uses_music_folder_in_home(tmp_path: Path) -> None:
    settings = pin_music_folder(Settings(_env_file=None))
    assert settings.resolved_output_dir == (Path.home() / "Music" / "TuneGrab").resolve()
    assert read_config_file() == {}  # nothing pinned for a fresh install


def test_saved_folder_is_kept(tmp_path: Path) -> None:
    chosen = tmp_path / "My Music"
    write_config_file({"output_dir": str(chosen)})
    settings = pin_music_folder(Settings(_env_file=None))
    assert settings.resolved_output_dir == chosen.resolve()
    assert read_config_file() == {"output_dir": str(chosen)}


def test_upgrade_with_history_pins_the_old_downloads_folder(tmp_path: Path) -> None:
    from tests.fakes import make_track
    from tunegrab.library.archive import Archive

    old = Settings(_env_file=None, output_dir=tmp_path / "downloads")
    with Archive(old.resolved_archive_path) as archive:
        archive.record_download(make_track(1), tmp_path / "downloads" / "a.mp3", "x", "mp3")
    settings = pin_music_folder(Settings(_env_file=None))  # cwd is tmp_path (conftest)
    expected = (tmp_path / "downloads").resolve()
    assert settings.resolved_output_dir == expected
    assert read_config_file() == {"output_dir": str(expected)}
    # and it never changes again, wherever TuneGrab is started from
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.chdir(elsewhere)
    assert pin_music_folder(Settings(_env_file=None)).resolved_output_dir == expected


def test_upgrade_without_history_but_with_music_pins_too(tmp_path: Path) -> None:
    (tmp_path / "downloads" / "Artist").mkdir(parents=True)
    settings = pin_music_folder(Settings(_env_file=None))
    assert settings.resolved_output_dir == (tmp_path / "downloads").resolve()


def test_relative_saved_folder_is_made_absolute_once(tmp_path: Path) -> None:
    write_config_file({"output_dir": "songs", "threads": 2})
    settings = pin_music_folder(Settings(_env_file=None))
    expected = (tmp_path / "songs").resolve()
    assert settings.resolved_output_dir == expected
    assert read_config_file() == {"output_dir": str(expected), "threads": 2}


def test_relative_folder_never_depends_on_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TUNEGRAB_OUTPUT_DIR", "Tunes")
    settings = pin_music_folder(Settings(_env_file=None))
    assert settings.resolved_output_dir == (Path.home() / "Tunes").resolve()
    assert read_config_file() == {}  # an environment choice isn't written anywhere


# --- format default (C10) ---------------------------------------------------------------
def _marker() -> Path:
    from tunegrab.config import DEFAULTS_MARKER, data_dir

    return data_dir() / DEFAULTS_MARKER


def test_fresh_profile_gets_m4a_and_keeps_it() -> None:
    settings = keep_earlier_defaults(Settings(_env_file=None))
    assert settings.format == "m4a" and settings.bitrate is None
    assert "format" not in read_config_file() and _marker().read_text() == "2"
    write_config_file({"threads": 2})  # later, the user saves something in Settings
    assert keep_earlier_defaults(Settings(_env_file=None)).format == "m4a"


def test_profile_with_saved_settings_keeps_mp3() -> None:
    write_config_file({"output_dir": "D:/Music", "threads": 6})  # saved by 0.4.x
    settings = keep_earlier_defaults(Settings(_env_file=None))
    assert settings.format == "mp3"
    assert read_config_file() == {"output_dir": "D:/Music", "threads": 6, "format": "mp3"}
    write_config_file({**read_config_file(), "format": "m4a"})  # they switch later
    assert keep_earlier_defaults(Settings(_env_file=None)).format == "m4a"


def test_profile_with_download_history_keeps_mp3(tmp_path: Path) -> None:
    from tests.fakes import make_track
    from tunegrab.library.archive import Archive

    settings = Settings(_env_file=None)
    with Archive(settings.resolved_archive_path) as archive:
        archive.record_download(make_track(1), tmp_path / "a.mp3", "x", "mp3")
    assert keep_earlier_defaults(settings).format == "mp3"
    assert read_config_file()["format"] == "mp3"


def test_a_chosen_format_is_never_changed() -> None:
    write_config_file({"format": "opus"})
    assert keep_earlier_defaults(Settings(_env_file=None)).format == "opus"
    assert read_config_file() == {"format": "opus"} and _marker().exists()
