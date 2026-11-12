"""TuneGrab: Spotify metadata -> YouTube audio -> tagged local files."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tunegrab")
except PackageNotFoundError:  # running from a source tree without installing
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
