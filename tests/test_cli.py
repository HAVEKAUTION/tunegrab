"""CLI commands end-to-end against FakeServices (no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.fakes import ALBUM_ID, PLAYLIST_ID, FakeServices, make_track
from tunegrab import __version__, cli
from tunegrab.config import Settings, read_config_file
from tunegrab.doctor import CheckResult, Status
from tunegrab.library.archive import Archive
from tunegrab.models import Track

runner = CliRunner()
PLAYLIST = f"https://open.spotify.com/playlist/{PLAYLIST_ID}"
ALBUM = f"https://open.spotify.com/album/{ALBUM_ID}"


@pytest.fixture
def services(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """Patch build_services; returns a holder so tests can tweak tracks/inspect state."""
    holder: dict[str, Any] = {"tracks": None, "made": []}

    def build(settings: Settings, http: Any = None) -> FakeServices:
        svc = FakeServices(settings, holder["tracks"])
        holder["made"].append(svc)
        return svc

    monkeypatch.setattr(cli, "build_services", build)
    monkeypatch.setenv("TUNEGRAB_OUTPUT_DIR", str(tmp_path / "lib"))
    monkeypatch.setenv("TUNEGRAB_LYRICS", "false")
    return holder


def invoke(*args: str, input: str | None = None) -> Any:
    result = runner.invoke(cli.app, list(args), input=input)
    return result


def files(tmp_path: Path) -> list[str]:
    root = tmp_path / "lib"
    return sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and ".tunegrab-tmp" not in p.parts
    )


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version(flag: str) -> None:
    result = invoke(flag)
    assert result.exit_code == 0 and result.output.strip() == f"tunegrab {__version__}"


def test_doctor_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    ok = [
        CheckResult("Python", Status.OK, "3.12"),
        CheckResult("Spotify", Status.WARN, "no", "hint"),
    ]
    monkeypatch.setattr(cli, "run_all", lambda s: ok)
    result = invoke("doctor")
    assert result.exit_code == 0 and "WARN" in result.output and "hint" in result.output
    monkeypatch.setattr(cli, "run_all", lambda s: [CheckResult("Deno", Status.FAIL, "x", "fix")])
    result = invoke("doctor")
    assert result.exit_code == 1 and "1 required check(s) failed" in result.output


def test_doctor_update(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "run_all", lambda s: [])
    monkeypatch.setattr(cli, "update_ytdlp", lambda: (True, "Updated yt-dlp"))
    assert "yt-dlp upgraded" in invoke("doctor", "--update").output
    monkeypatch.setattr(cli, "update_ytdlp", lambda: (False, "nope"))
    assert invoke("doctor", "--update").exit_code == 1


def test_info(services: dict[str, Any]) -> None:
    result = invoke("info", ALBUM)
    assert result.exit_code == 0, result.output
    assert (
        "Fake Album" in result.output and "Song 3" in result.output and "3 tracks" in result.output
    )


def test_info_bad_url(services: dict[str, Any]) -> None:
    result = invoke("info", "https://example.com/nope")
    assert result.exit_code == 1 and "Error" in result.output


def test_download_playlist_and_rerun(services: dict[str, Any], tmp_path: Path) -> None:
    result = invoke("download", PLAYLIST)
    assert result.exit_code == 0, result.output
    assert "Fake Playlist" in result.output and "Playlist file" in result.output
    assert files(tmp_path) == [
        "Artist 1/Album 1 (2020)/01 - Song 1.mp3",
        "Artist 2/Album 2 (2020)/02 - Song 2.mp3",
        "Artist 3/Album 3 (2020)/03 - Song 3.mp3",
        "Playlists/Fake Playlist.m3u8",
    ]
    rerun = invoke("download", PLAYLIST)
    assert rerun.exit_code == 0 and services["made"][-1].downloader.calls == 0


def test_download_from_file_and_bad_inputs(services: dict[str, Any], tmp_path: Path) -> None:
    listing = tmp_path / "list.txt"
    listing.write_text(f"# comment\n\n{ALBUM}\nhttps://open.spotify.com/show/{'x' * 22}\n")
    result = invoke("download", "--from-file", str(listing))
    assert result.exit_code == 0, result.output
    assert "Could not resolve" in result.output and len(files(tmp_path)) == 3
    assert invoke("download").exit_code == 1


def test_download_failure_exit_code_and_retry(
    services: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TUNEGRAB_RETRIES", "0")
    original = FakeServices.pipeline

    def failing(self: FakeServices, *a: Any, **k: Any) -> Any:
        self.downloader.fail_times = 99
        return original(self, *a, **k)

    monkeypatch.setattr(FakeServices, "pipeline", failing)
    result = invoke("download", ALBUM)
    assert result.exit_code == 1 and "Failed tracks" in result.output
    assert "tunegrab retry" in result.output
    status = invoke("status")
    assert "Open failures" in status.output and "network hiccup" in status.output

    monkeypatch.setattr(FakeServices, "pipeline", original)
    retry = invoke("retry")
    assert retry.exit_code == 0, retry.output
    assert "Retrying 3 track(s)" in retry.output and len(files(tmp_path)) == 3
    assert "No failed tracks" in invoke("retry").output


def test_retry_clear(services: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TUNEGRAB_RETRIES", "0")
    original = FakeServices.pipeline

    def failing(self: FakeServices, *a: Any, **k: Any) -> Any:
        self.downloader.fail_times = 99
        return original(self, *a, **k)

    monkeypatch.setattr(FakeServices, "pipeline", failing)
    invoke("download", ALBUM)
    assert "Cleared 3" in invoke("retry", "--clear").output


def test_get_query(services: dict[str, Any], tmp_path: Path) -> None:
    result = invoke("get", "Found Artist - Found Song")
    assert result.exit_code == 0, result.output
    assert "Saved:" in result.output
    assert files(tmp_path) == ["Found Artist/Found Album/Found Song.mp3"]


def test_search_pick(services: dict[str, Any], tmp_path: Path) -> None:
    result = invoke("search", "anything", input="1\n")
    assert result.exit_code == 0, result.output
    assert "Found Song" in result.output and len(files(tmp_path)) == 1
    cancelled = invoke("search", "anything", input="0\n")
    assert "Cancelled" in cancelled.output


def test_match_table(services: dict[str, Any]) -> None:
    result = invoke("match", ALBUM)
    assert result.exit_code == 0, result.output
    assert "3/3 above threshold" in result.output and "youtube.com/watch" in result.output


def test_sync_lists_then_removes(services: dict[str, Any], tmp_path: Path) -> None:
    services["tracks"] = [make_track(1), make_track(2), make_track(3)]
    assert invoke("sync", PLAYLIST).exit_code == 0
    assert len(files(tmp_path)) == 4
    services["tracks"] = [make_track(1), make_track(4)]  # 2 and 3 left the playlist
    result = invoke("sync", PLAYLIST)
    assert result.exit_code == 0, result.output
    assert (
        "2 track(s) are no longer in the playlist" in result.output and "--remove" in result.output
    )
    assert len(files(tmp_path)) == 5  # nothing deleted, track 4 added
    services["tracks"] = [make_track(1), make_track(4)]
    # removal needs confirmation; "n" keeps the files
    no = invoke("sync", PLAYLIST, "--remove", input="n\n")
    assert no.exit_code == 0 and len(files(tmp_path)) == 5
    yes = invoke("sync", PLAYLIST, "--remove", "--yes")
    assert "Deleted 2 of 2 file(s)" in yes.output, yes.output
    remaining = files(tmp_path)
    assert remaining == [
        "Artist 1/Album 1 (2020)/01 - Song 1.mp3",
        "Artist 4/Album 4 (2020)/04 - Song 4.mp3",
        "Playlists/Fake Playlist.m3u8",
    ]
    m3u = (tmp_path / "lib" / "Playlists" / "Fake Playlist.m3u8").read_text("utf-8")
    assert "Song 1" in m3u and "Song 4" in m3u and "Song 2" not in m3u


def test_sync_rejects_albums(services: dict[str, Any]) -> None:
    result = invoke("sync", ALBUM)
    assert result.exit_code == 1 and "playlists" in result.output


def test_fix_overrides_and_redownloads(services: dict[str, Any], tmp_path: Path) -> None:
    track: Track = make_track(1)
    services["tracks"] = [track]
    invoke("download", f"spotify:track:{track.spotify_id}")
    result = invoke(
        "fix", f"spotify:track:{track.spotify_id}", "https://youtu.be/dQw4w9WgXcQ", "--yes"
    )
    assert result.exit_code == 0, result.output
    svc = services["made"][-1]
    svc.archive = Archive(svc.archive.path)  # the command closed its connection
    hit = svc.archive.get_match(track.key)
    assert hit and hit[0].video_id == "dQw4w9WgXcQ" and svc.archive.is_manual_match(track.key)
    record = svc.archive.get_download(track.key)
    assert record and record.video_id == "dQw4w9WgXcQ" and record.exists
    assert len(files(tmp_path)) == 1  # old file replaced, not duplicated


def test_fix_keeps_the_old_file_when_the_new_download_fails(
    services: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track: Track = make_track(1)
    services["tracks"] = [track]
    invoke("download", f"spotify:track:{track.spotify_id}")
    [old] = files(tmp_path)
    old_bytes = (tmp_path / "lib" / old).read_bytes()
    build = cli.build_services

    def broken(settings: Settings, http: Any = None) -> FakeServices:
        svc = build(settings, http)
        svc.downloader.unavailable.add("dQw4w9WgXcQ")
        return svc

    monkeypatch.setattr(cli, "build_services", broken)
    result = invoke(
        "fix", f"spotify:track:{track.spotify_id}", "https://youtu.be/dQw4w9WgXcQ", "--yes"
    )
    assert result.exit_code == 1
    assert "old file is back in place" in result.output
    assert files(tmp_path) == [old]  # no leftover backup, nothing lost
    assert (tmp_path / "lib" / old).read_bytes() == old_bytes
    svc = services["made"][-1]
    svc.archive = Archive(svc.archive.path)
    record = svc.archive.get_download(track.key)
    assert record is not None and record.exists  # still known as downloaded


def test_fix_reports_a_locked_old_file(
    services: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track: Track = make_track(1)
    services["tracks"] = [track]
    invoke("download", f"spotify:track:{track.spotify_id}")
    [old] = files(tmp_path)

    def locked(self: Path, target: Any) -> Path:
        raise PermissionError(13, "The file is in use", str(self))

    monkeypatch.setattr(Path, "replace", locked)
    result = invoke(
        "fix", f"spotify:track:{track.spotify_id}", "https://youtu.be/dQw4w9WgXcQ", "--yes"
    )
    assert result.exit_code == 1 and "close it and try again" in result.output
    assert "Traceback" not in result.output
    assert files(tmp_path) == [old]


def test_sync_remove_reports_locked_files(
    services: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    services["tracks"] = [make_track(1), make_track(2)]
    assert invoke("sync", PLAYLIST).exit_code == 0
    services["tracks"] = [make_track(1)]
    invoke("sync", PLAYLIST)
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self.suffix == ".mp3":
            raise PermissionError(13, "The file is in use", str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    result = invoke("sync", PLAYLIST, "--remove", "--yes")
    assert result.exit_code == 1, result.output
    assert "Couldn't delete" in result.output and "Deleted 0 of 1" in result.output
    assert "Traceback" not in result.output
    monkeypatch.setattr(Path, "unlink", real_unlink)
    retry = invoke("sync", PLAYLIST, "--remove", "--yes")  # still pending, works once closed
    assert retry.exit_code == 0 and "Deleted 1 of 1" in retry.output, retry.output


def test_login_network_failure_is_a_message(monkeypatch: pytest.MonkeyPatch) -> None:
    from tunegrab.metadata import spotify_api
    from tunegrab.net import NetworkError

    def offline(*args: Any, **kwargs: Any) -> None:
        raise NetworkError("connection refused")

    monkeypatch.setattr(spotify_api, "login", offline)
    result = invoke("login")
    assert result.exit_code == 1 and "Couldn't reach Spotify" in result.output
    assert "Traceback" not in result.output


def test_fix_validates_input(services: dict[str, Any]) -> None:
    assert invoke("fix", ALBUM, "dQw4w9WgXcQ").exit_code == 1
    assert invoke("fix", f"spotify:track:{'1' * 22}", "not a video").exit_code == 1


def test_config_commands() -> None:
    assert invoke("config", "set", "threads", "6").exit_code == 0
    assert invoke("config", "set", "format", "opus").exit_code == 0
    assert read_config_file() == {"threads": 6, "format": "opus"}
    shown = invoke("config", "show")
    assert "opus" in shown.output and "config.toml" in shown.output.replace("\n", "")
    assert invoke("config", "set", "threads", "999").exit_code == 1
    assert invoke("config", "set", "bogus", "1").exit_code == 1
    assert invoke("config", "set", "track_template", "{nope}.{ext}").exit_code == 1
    assert invoke("config", "unset", "threads").exit_code == 0
    assert read_config_file() == {"format": "opus"}
    assert invoke("config", "set", "format", "none").exit_code == 0
    assert read_config_file() == {}
    # keyring is disabled in tests -> clear error, nothing written
    secret = invoke("config", "set", "spotify_client_secret", "s3cret")
    assert secret.exit_code == 1 and ".env" in secret.output and read_config_file() == {}
    assert invoke("config", "path").output.strip().endswith("config.toml")


def test_bad_template_from_config(
    services: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TUNEGRAB_TRACK_TEMPLATE", "{title}")
    result = invoke("download", ALBUM)
    assert result.exit_code == 1 and "template" in result.output


def test_logout_without_keyring() -> None:
    result = invoke("logout")
    assert result.exit_code == 1 and "keyring" in result.output


def test_login_needs_client_id() -> None:
    result = invoke("login")
    assert result.exit_code == 1 and "SPOTIFY_CLIENT_ID" in result.output


def test_serve_reuses_running_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    monkeypatch.setattr(cli, "_port_open", lambda h, p: True)
    monkeypatch.setattr(cli, "_is_tunegrab", lambda url: True)
    result = invoke("serve")
    assert result.exit_code == 0 and "already running" in result.output
    assert opened == ["http://127.0.0.1:8765/"]
    monkeypatch.setattr(cli, "_is_tunegrab", lambda url: False)
    busy = invoke("serve", "--no-open")
    assert busy.exit_code == 1 and "--port 8766" in busy.output


def test_browser_opens_only_when_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    answers = iter([False, False, True])
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    monkeypatch.setattr(cli, "_port_open", lambda h, p: next(answers))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    cli._open_when_ready("127.0.0.1", 8765, "http://127.0.0.1:8765/")
    assert opened == ["http://127.0.0.1:8765/"]


def test_video_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from tunegrab import video
    from tunegrab.video import VideoEntry, VideoProbe

    entry = VideoEntry("dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ", "Song", "Chan", 213)
    found = VideoProbe("u", "video", "Song", "Chan", "Youtube", [entry])
    monkeypatch.setattr(cli, "probe", lambda url, cookies=None: found)

    def fake_download(self: Any, url: str, dest: Path, progress: Any = None, cancel: Any = None):  # type: ignore[no-untyped-def]
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / "x.mp4"
        out.write_bytes(b"v")
        return out, "1080p H.264 + AAC 130 kbps", False

    monkeypatch.setattr(video.VideoDownloader, "download", fake_download)
    monkeypatch.setattr(video, "cut_clip", lambda path, options, cancel: path)
    result = invoke("video", "https://youtu.be/dQw4w9WgXcQ", "-o", str(tmp_path), "-s", "0:10")
    assert result.exit_code == 0, result.output
    assert (tmp_path / "Chan" / "Song (clip 0m10s-end) [dQw4w9WgXcQ].mp4").exists()
    assert "1080p H.264" in result.output
    bad = invoke("video", "https://youtu.be/dQw4w9WgXcQ", "-o", str(tmp_path), "-s", "9:00")
    assert bad.exit_code == 1 and "only 3:33 long" in bad.output


def test_app_neutral_messages_get_cli_hints() -> None:
    from tunegrab.cli import cli_hint

    assert cli_hint("Liked Songs need a Spotify login.") == "Log in with: tunegrab login"
    assert "tunegrab fix" in cli_hint("no match above 70 (best 52: title 40)")
    assert "docs/SETUP.md" in cli_hint(
        "Spotify rejected the client ID/secret. Check SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET"
    )
    assert cli_hint("Video unavailable") == ""
    from tunegrab.download.ytdlp import BREAKAGE_HINT

    assert "tunegrab doctor --update" in cli_hint(
        f"YouTube refused this download (x). {BREAKAGE_HINT}"
    )


def test_no_user_facing_message_names_the_cli_or_dotenv() -> None:
    """Messages that reach the web app must not send people to a terminal or .env (M8)."""
    import re
    from pathlib import Path

    import tunegrab

    src = Path(tunegrab.__file__).parent
    offenders = []
    for path in src.rglob("*.py"):
        if path.name == "cli.py" or path.name == "ui.py":
            continue  # the CLI's own output may name commands
        text = path.read_text(encoding="utf-8")
        for literal in re.findall(r'"[^"\n]*"|\'[^\'\n]*\'', text):
            if re.search(r"`tunegrab \w|run: tunegrab|\.env\b|docs/", literal):
                offenders.append(f"{path.name}: {literal}")
    allowed = {
        'config.py: ".env"',  # the settings source itself
        'config.py: "# TuneGrab settings. Edit by hand or with `tunegrab config set KEY VALUE`."',
        'doctor.py: \'Update it: Settings > System > "Update yt-dlp", or run: '
        "tunegrab doctor --update'",  # names the app path first
        'youtube.py: "Full metadata for one video (used by ``tunegrab fix``)."',  # docstring
        'spotify_api.py: "Run `tunegrab login` again."',  # log line
        'spotify_api.py: "Spotify user token refresh failed (HTTP %s); run `tunegrab login`"',
        'spotify_api.py: "Set SPOTIFY_CLIENT_ID first (docs/SETUP.md)."',  # `tunegrab login` only
    }
    assert sorted(set(offenders) - allowed) == []


def test_history_from_a_newer_version_is_a_message() -> None:
    settings = Settings(_env_file=None)
    with Archive(settings.resolved_archive_path) as archive:
        archive.set_meta("schema_version", 99)
    result = invoke("status")
    assert result.exit_code == 1 and "newer TuneGrab" in result.output
    assert "Traceback" not in result.output
