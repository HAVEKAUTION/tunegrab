"""Settings (pydantic-settings): paths, format, bitrate, templates, credentials.

Precedence, highest first: CLI flags (passed as init kwargs) > environment variables
> ``.env`` (current dir, then the user config dir) > OS keyring (secret only)
> ``config.toml`` in the user config dir > defaults.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from platformdirs import user_config_path, user_data_path, user_log_path
from pydantic import Field, SecretStr, TypeAdapter, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

log = logging.getLogger(__name__)

APP_NAME = "tunegrab"
KEYRING_SERVICE = "tunegrab"
SECRET_KEYS = frozenset({"spotify_client_secret"})

AudioFormat = Literal["mp3", "m4a", "opus", "flac"]
PlaylistMode = Literal["m3u", "folder"]
VideoQuality = Literal["best", "2160", "1440", "1080", "720", "480", "360"]

DEFAULT_TRACK_TEMPLATE = "{album_artist}/{album} ({year})/{track_number:02d} - {title}.{ext}"
DEFAULT_PLAYLIST_TEMPLATE = "Playlists/{playlist}/{position:03d} - {artist} - {title}.{ext}"


def config_dir() -> Path:
    override = os.environ.get("TUNEGRAB_CONFIG_DIR")
    return Path(override) if override else user_config_path(APP_NAME, appauthor=False)


def config_file() -> Path:
    return config_dir() / "config.toml"


def data_dir() -> Path:
    override = os.environ.get("TUNEGRAB_DATA_DIR")
    return Path(override) if override else user_data_path(APP_NAME, appauthor=False)


def log_dir() -> Path:
    override = os.environ.get("TUNEGRAB_LOG_DIR")
    return Path(override) if override else user_log_path(APP_NAME, appauthor=False)


# Problems found and repaired at startup (a broken settings file or database), shown once
# in the app and the CLI so the user knows where their old file went.
startup_warnings: list[str] = []


def set_aside(path: Path, label: str) -> Path:
    """Rename an unreadable file to ``<name>.<label>-<timestamp>`` (never delete it)."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.{label}-{stamp}")
    path.replace(target)
    return target


def recover_config_file(path: Path | None = None) -> None:
    """If config.toml can't be parsed, move it aside so TuneGrab starts with defaults."""
    path = path or config_file()
    if not path.exists():
        return
    try:
        with path.open("rb") as handle:
            tomllib.load(handle)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        try:
            kept = set_aside(path, "broken")
        except OSError:
            log.exception("could not move the unreadable settings file %s", path)
            raise
        log.warning("settings file %s was unreadable (%s); kept as %s", path, exc, kept)
        startup_warnings.append(
            f"Your settings file couldn't be read ({exc}), so TuneGrab started with default "
            f"settings. The old file was kept as {kept}."
        )


def keyring_enabled() -> bool:
    return os.environ.get("TUNEGRAB_NO_KEYRING", "") not in ("1", "true", "yes")


def keyring_get(name: str) -> str | None:
    if not keyring_enabled():
        return None
    try:
        import keyring

        value: str | None = keyring.get_password(KEYRING_SERVICE, name)
    except Exception:  # no backend, locked vault, ... treat as "not stored"
        return None
    return value


def keyring_set(name: str, value: str | None) -> None:
    """Store (or with ``None`` delete) a secret. Raises RuntimeError if unavailable."""
    if not keyring_enabled():
        raise RuntimeError("keyring disabled via TUNEGRAB_NO_KEYRING")
    import keyring
    import keyring.errors

    try:
        if value is None:
            with contextlib.suppress(keyring.errors.PasswordDeleteError):
                keyring.delete_password(KEYRING_SERVICE, name)
        else:
            keyring.set_password(KEYRING_SERVICE, name, value)
    except keyring.errors.KeyringError as exc:
        raise RuntimeError(f"OS keyring unavailable: {exc}") from exc


class _KeyringSource(PydanticBaseSettingsSource):
    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        secret = keyring_get("spotify_client_secret")
        return {"spotify_client_secret": secret} if secret else {}


# browsers yt-dlp can read YouTube cookies from (the CLI also accepts "firefox:Profile")
COOKIE_BROWSERS = ("firefox", "chrome", "edge", "brave", "safari", "opera", "vivaldi")


INSTALL_DIR = (
    Path(__file__).resolve().parents[2]
)  # the folder install.ps1 / a clone puts TuneGrab in
ENV_FILES = (config_dir() / ".env", INSTALL_DIR / ".env")


class Settings(BaseSettings):
    """Application settings."""

    model_config = SettingsConfigDict(
        env_prefix="TUNEGRAB_",
        # only TuneGrab's own .env files, never one in whatever folder it was started from
        env_file=ENV_FILES,
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        validate_default=True,
    )

    # None: ~/Music/TuneGrab. A relative path is relative to your home folder (never to
    # wherever TuneGrab happened to be started from).
    output_dir: Path | None = None
    # M4A keeps YouTube's original AAC stream (no re-encode) and plays everywhere modern.
    # Profiles from before 0.5 keep MP3 (pinned once by pin_format).
    format: AudioFormat = "m4a"
    # kbps for MP3 (constant bitrate); None: LAME VBR V0, ~245 kbps, as good as 320 for
    # YouTube's 128-160 kbps sources and smaller
    bitrate: int | None = Field(default=None, ge=32, le=320)
    track_template: str = DEFAULT_TRACK_TEMPLATE
    playlist_template: str = DEFAULT_PLAYLIST_TEMPLATE
    playlist_mode: PlaylistMode = "m3u"  # m3u: .m3u8 into the library; folder: copies
    threads: int = Field(default=4, ge=1, le=16)
    retries: int = Field(default=2, ge=0, le=10)
    lyrics: bool = True
    lrc: bool = False  # write a synced .lrc sidecar when available
    match_threshold: float = Field(default=70.0, ge=0, le=100)
    archive_path: Path | None = None
    # e.g. "firefox" / "chrome": lets yt-dlp use your browser's YouTube sign-in (cookies)
    # for age-restricted videos. Off by default: YouTube can restrict the account used.
    cookies_from_browser: str | None = None
    # once a day, ask GitHub and PyPI whether a newer TuneGrab / yt-dlp is out
    check_updates: bool = True
    video_quality: VideoQuality = "1080"  # highest video height to download
    video_dir: Path | None = None  # default: <output_dir>/Videos

    spotify_client_id: str | None = Field(default=None, validation_alias="SPOTIFY_CLIENT_ID")
    spotify_client_secret: SecretStr | None = Field(
        default=None, validation_alias="SPOTIFY_CLIENT_SECRET"
    )
    spotify_redirect_uri: str = Field(
        default="http://127.0.0.1:8888/callback", validation_alias="SPOTIFY_REDIRECT_URI"
    )

    @field_validator("spotify_client_id", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("spotify_client_secret", mode="before")
    @classmethod
    def _blank_secret_is_none(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        recover_config_file()
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _KeyringSource(settings_cls),
            TomlConfigSettingsSource(settings_cls, toml_file=config_file()),
        )

    @property
    def has_spotify_credentials(self) -> bool:
        secret = self.spotify_client_secret.get_secret_value() if self.spotify_client_secret else ""
        return bool(self.spotify_client_id) and bool(secret)

    @property
    def resolved_archive_path(self) -> Path:
        return self.archive_path or data_dir() / "tunegrab.db"

    @property
    def resolved_output_dir(self) -> Path:
        if self.output_dir is None:
            return default_music_dir()
        folder = self.output_dir.expanduser()
        return (folder if folder.is_absolute() else Path.home() / folder).resolve()

    @property
    def resolved_video_dir(self) -> Path:
        if self.video_dir is not None:
            return self.video_dir.expanduser().resolve()
        return self.resolved_output_dir / "Videos"


# --- reading / writing config.toml -------------------------------------------

SETTABLE_KEYS: tuple[str, ...] = tuple(Settings.model_fields)


def default_music_dir() -> Path:
    """Where a fresh install saves music."""
    return (Path.home() / "Music" / "TuneGrab").resolve()


# 0.4.x saved music to "downloads" next to wherever TuneGrab was started (the install
# folder, for the launcher and installer).
LEGACY_OUTPUT_DIR = "downloads"


def _history_has_downloads(archive: Path) -> bool:
    if not archive.is_file():
        return False
    try:
        conn = sqlite3.connect(f"file:{archive.as_posix()}?mode=ro", uri=True)
        try:
            return conn.execute("SELECT 1 FROM downloads LIMIT 1").fetchone() is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False


LEGACY_FORMAT: AudioFormat = "mp3"
# Bumped when a default changes in a way existing users must not notice. Written to the
# data folder once, so a profile is judged "existing" only the first time a new version runs.
DEFAULTS_VERSION = 2  # 2: M4A instead of MP3 (0.5)
DEFAULTS_MARKER = "defaults-version"


def _defaults_seen() -> int:
    try:
        return int((data_dir() / DEFAULTS_MARKER).read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def _legacy_folder_used(cwd: Path) -> bool:
    legacy = cwd / LEGACY_OUTPUT_DIR
    try:
        return legacy.is_dir() and any(legacy.iterdir())
    except OSError:
        return False


def pin_format(settings: Settings) -> Settings:
    """Keep MP3 for people who used TuneGrab before M4A became the default (C10).

    The first time this version runs, a profile with saved settings, a used ``downloads``
    folder or a download history gets ``format = "mp3"`` saved (unless it already chose
    one); a fresh profile keeps the new default. Either way a marker is written to the data
    folder, so later changes in Settings never flip the format.
    """
    if _defaults_seen() >= DEFAULTS_VERSION:
        return settings
    try:
        saved = read_config_file()
    except (OSError, tomllib.TOMLDecodeError):
        return settings  # recover_config_file() reports a broken file
    existing = "format" not in saved and (
        bool(saved)
        or _legacy_folder_used(Path.cwd())
        or _history_has_downloads(settings.resolved_archive_path)
    )
    try:
        if existing:
            write_config_file({**saved, "format": LEGACY_FORMAT})
        marker = data_dir() / DEFAULTS_MARKER
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(DEFAULTS_VERSION), encoding="utf-8")
    except OSError as exc:  # decided again next start, with the same answer
        log.warning("could not save the format default: %s", exc)
    if existing and "format" not in settings.model_fields_set:
        log.info("kept MP3, the format of earlier versions")
        return settings.model_copy(update={"format": LEGACY_FORMAT})
    return settings


def keep_earlier_defaults(settings: Settings) -> Settings:
    """Startup: changed defaults (format, music folder) never surprise an existing user."""
    return pin_music_folder(pin_format(settings))


def pin_music_folder(settings: Settings) -> Settings:
    """Keep existing users' music where it is when the default folder changes (C1).

    * A relative ``output_dir`` saved by 0.4.x meant "relative to where TuneGrab was
      started": it is rewritten once as the absolute folder it pointed to.
    * No saved folder, but a download history with songs (or a non-empty ``downloads``
      folder here): that old default is saved, so it never changes under the user.
    * Otherwise (a fresh install) nothing is saved and ~/Music/TuneGrab is used.
    """
    try:
        saved = read_config_file()
    except (OSError, tomllib.TOMLDecodeError):
        return settings  # recover_config_file() reports a broken file
    cwd = Path.cwd()
    if "output_dir" in saved:
        folder = Path(str(saved["output_dir"])).expanduser()
        if folder.is_absolute():
            return settings
        pinned = (cwd / folder).resolve()
        write_config_file({**saved, "output_dir": str(pinned)})
        log.info("music folder %s saved as %s", folder, pinned)
        if settings.output_dir == Path(str(saved["output_dir"])):
            return settings.model_copy(update={"output_dir": pinned})
        return settings
    if "output_dir" in settings.model_fields_set:
        return settings  # chosen for this run (environment or a command-line option)
    legacy = cwd / LEGACY_OUTPUT_DIR
    if not (_legacy_folder_used(cwd) or _history_has_downloads(settings.resolved_archive_path)):
        return settings
    pinned = legacy.resolve()
    write_config_file({**saved, "output_dir": str(pinned)})
    log.info("kept the music folder of an earlier version: %s", pinned)
    return settings.model_copy(update={"output_dir": pinned})


def read_config_file(path: Path | None = None) -> dict[str, Any]:
    path = path or config_file()
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    return json.dumps(str(value), ensure_ascii=False)  # JSON strings are valid TOML strings


def write_config_file(values: dict[str, Any], path: Path | None = None) -> Path:
    path = path or config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# TuneGrab settings. Edit by hand or with `tunegrab config set KEY VALUE`.",
        "# TuneGrab rewrites this file when settings are saved, so comments added here are lost.",
    ]
    lines += [f"{key} = {_toml_value(values[key])}" for key in sorted(values)]
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def coerce_setting(key: str, raw: str) -> Any:
    """Validate a string from the command line against the field's type."""
    if key not in Settings.model_fields:
        raise KeyError(key)
    field = Settings.model_fields[key]
    value: Any = raw
    if raw.strip().lower() in ("none", "null", ""):
        return None  # "unset": back to the default
    adapter: TypeAdapter[Any] = TypeAdapter(field.annotation or str)
    validated = adapter.validate_python(value)
    if validated is not None:
        for meta in field.metadata:  # numeric bounds from Field(ge=..., le=...)
            low = getattr(meta, "ge", None)
            high = getattr(meta, "le", None)
            if low is not None and validated < low:
                raise ValueError(f"{key} must be >= {low}")
            if high is not None and validated > high:
                raise ValueError(f"{key} must be <= {high}")
    if isinstance(validated, Path):
        return str(validated)
    if isinstance(validated, SecretStr):
        return validated.get_secret_value()
    return validated
