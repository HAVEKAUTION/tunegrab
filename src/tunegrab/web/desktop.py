"""The bits of the web app that act on the desktop of the machine running it."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def open_in_file_manager(path: Path) -> None:
    """Show ``path`` (a folder, or a file selected in its folder) in Explorer/Finder/etc."""
    if sys.platform == "win32":
        if path.is_file():
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            os.startfile(path)  # type: ignore[attr-defined,unused-ignore]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)] if path.is_file() else ["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path if path.is_dir() else path.parent)])


def pick_folder(initial: Path) -> str | None:
    """Native folder picker on the machine running the server (it's always local)."""
    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        chosen = filedialog.askdirectory(
            initialdir=str(initial if initial.exists() else Path.home()),
            title="Choose where TuneGrab saves music",
            parent=root,
        )
    finally:
        root.destroy()
    return chosen or None
