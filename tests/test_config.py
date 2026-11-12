from pathlib import Path

import pytest

from tunegrab.config import (
    Settings,
    coerce_setting,
    config_file,
    read_config_file,
    write_config_file,
)


def test_defaults() -> None:
    s = Settings(_env_file=None)
    assert (s.format, s.bitrate, s.threads, s.playlist_mode, s.lyrics) == (
        "mp3",
        320,
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
    (tmp_path / "config.toml").write_text('format = "m4a\nthreads = = 2\n', encoding="utf-8")
    settings = Settings(_env_file=None)
    assert settings.format == "mp3"  # defaults
    kept = list(tmp_path.glob("config.toml.broken-*"))
    assert len(kept) == 1 and "threads = = 2" in kept[0].read_text(encoding="utf-8")
    assert not (tmp_path / "config.toml").exists()
    assert len(config.startup_warnings) == 1 and str(kept[0]) in config.startup_warnings[0]
