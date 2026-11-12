"""Write ``.m3u8`` playlist files that point into the library."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from tunegrab.library.paths import sanitize_segment
from tunegrab.models import Track


def playlist_file(output_dir: Path, name: str) -> Path:
    return output_dir / "Playlists" / f"{sanitize_segment(name)}.m3u8"


def _relative(target: Path, start: Path) -> str:
    try:
        return Path(os.path.relpath(target, start)).as_posix()
    except ValueError:  # different drive on Windows
        return str(target)


def write_m3u8(path: Path, name: str, entries: Iterable[tuple[Path, Track]]) -> Path:
    """Write an extended M3U (UTF-8) with paths relative to the playlist file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U", f"#PLAYLIST:{name}"]
    for file_path, track in entries:
        lines.append(f"#EXTINF:{round(track.duration_s)},{track.display}")
        lines.append(_relative(file_path.resolve(), path.parent.resolve()))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def read_m3u8(path: Path) -> list[Path]:
    """Resolved file paths listed in an M3U file."""
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            candidate = Path(line)
            entries.append(candidate if candidate.is_absolute() else (path.parent / candidate))
    return [p.resolve() for p in entries]
