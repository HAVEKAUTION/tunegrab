"""Logging: a rotating file in the user log dir; ``--verbose`` also logs to stderr."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from rich.logging import RichHandler

from tunegrab.config import log_dir

_configured = False


def log_file() -> Path:
    return log_dir() / "tunegrab.log"


def setup_logging(verbose: bool = False) -> Path | None:
    """Idempotent. Returns the log file path (None if it couldn't be created)."""
    global _configured
    root = logging.getLogger()
    if _configured:
        return log_file()
    _configured = True
    root.setLevel(logging.DEBUG)
    path: Path | None = log_file()
    try:
        assert path is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)s %(name)s: %(message)s")
        )
        root.addHandler(handler)
    except OSError:
        path = None
    console = RichHandler(show_path=False, rich_tracebacks=False, markup=False)
    console.setLevel(logging.DEBUG if verbose else logging.ERROR)
    root.addHandler(console)
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return path
