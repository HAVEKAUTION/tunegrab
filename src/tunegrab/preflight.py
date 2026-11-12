"""Checks that stop a whole job early (missing folder, full disk) instead of failing every
song separately with a raw OS error."""

from __future__ import annotations

import errno
import re
import shutil
from pathlib import Path

from tunegrab.doctor import Status, check_output_dir
from tunegrab.errors import TuneGrabError

MIN_FREE_BYTES = 200 * 1024 * 1024  # a few albums' worth of headroom
DISK_FULL = "Your disk is full. Free some space, then press Retry."
_DISK_FULL_TEXT = re.compile(
    r"no space left on device|not enough space on the disk|disk (?:is )?full", re.IGNORECASE
)


class JobAborted(TuneGrabError):
    """The job can't continue; ``str(exc)`` is a sentence for the user."""


def _existing(path: Path) -> Path:
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def check_folder(root: Path, what: str = "music folder") -> None:
    """Raise :class:`JobAborted` if ``root`` can't take new files right now."""
    result = check_output_dir(root)
    if result.status is Status.FAIL:
        raise JobAborted(
            f"Your {what} {root} isn't available ({result.detail}). Reconnect the drive or "
            "choose another folder in Settings, then try again."
        )
    try:
        free = shutil.disk_usage(_existing(root)).free
    except OSError:
        return  # can't tell; the download itself will report a real problem
    if free < MIN_FREE_BYTES:
        raise JobAborted(DISK_FULL)


def is_disk_full(exc: BaseException) -> bool:
    """True if ``exc`` (or anything it wraps) says the disk ran out of space."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError) and current.errno == errno.ENOSPC:
            return True
        if _DISK_FULL_TEXT.search(str(current)):
            return True
        current = current.__cause__ or current.__context__
    return False
