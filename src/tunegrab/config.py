"""Settings (pydantic-settings): paths, format, bitrate, templates, credentials.

Precedence, highest first: CLI flags (passed as init kwargs) > environment variables
> ``.env`` (current dir, then the user config dir) > OS keyring (secret only)
> ``config.toml`` in the user config dir > defaults.
"""

from __future__ import annotations

import contextlib
import json
import os
import tomllib
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


class Settings(BaseSettings):
    """Application settings."""

    model_config = SettingsConfigDict(
        env_prefix="TUNEGRAB_",
        env_file=(config_dir() / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        validate_default=True,
    )

    output_dir: Path = Path("downloads")
    format: AudioFormat = "mp3"
    bitrate: int = Field(default=320, ge=32, le=320)  # kbps, mp3 only
    track_template: str = DEFAULT_TRACK_TEMPLATE
    playlist_template: str = DEFAULT_PLAYLIST_TEMPLATE
    playlist_mode: PlaylistMode = "m3u"  # m3u: .m3u8 into the library; folder: copies
    threads: int = Field(default=4, ge=1, le=16)
    retries: int = Field(default=2, ge=0, le=10)
    lyrics: bool = True
    lrc: bool = False  # write a synced .lrc sidecar when available
    match_threshold: float = Field(default=70.0, ge=0, le=100)
    archive_path: Path | None = None
    # e.g. "firefox" / "chrome": lets yt-dlp use your browser's YouTube cookies for
    # age-restricted videos.
    cookies_from_browser: str | None = None
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
        return self.output_dir.expanduser().resolve()

    @property
    def resolved_video_dir(self) -> Path:
        if self.video_dir is not None:
            return self.video_dir.expanduser().resolve()
        return self.resolved_output_dir / "Videos"


# --- reading / writing config.toml -------------------------------------------

SETTABLE_KEYS: tuple[str, ...] = tuple(Settings.model_fields)


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
    lines = ["# TuneGrab settings. Edit by hand or with `tunegrab config set KEY VALUE`."]
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
