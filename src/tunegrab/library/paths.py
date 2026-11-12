"""Filename templates and cross-platform sanitisation.

Template fields: ``{title} {artist} {artists} {album} {album_artist} {year} {date}
{track_number} {disc_number} {total_tracks} {playlist} {position} {isrc} {ext}``.
Format specs work (``{track_number:02d}``); a missing value renders as nothing and the
leftover ``()`` / dangling `` - `` are tidied away.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import string
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tunegrab.models import Track

TEMPLATE_FIELDS = frozenset(
    {
        "title",
        "artist",
        "artists",
        "album",
        "album_artist",
        "year",
        "date",
        "track_number",
        "disc_number",
        "total_tracks",
        "playlist",
        "position",
        "isrc",
        "ext",
    }
)
MAX_SEGMENT_CHARS = 120
MAX_SEGMENT_BYTES = 240  # most filesystems cap a name at 255 bytes
MAX_PATH_CHARS = 250  # stay under Windows' classic 260 limit

_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_REPLACEMENTS = {
    "/": "-",
    "\\": "-",
    "|": "-",
    ":": " -",
    "*": "",
    "?": "",
    '"': "'",
    "<": "",
    ">": "",
}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_EMPTY_BRACKETS = re.compile(r"\(\s*\)|\[\s*\]|\{\s*\}")
_LEADING_SEP = re.compile(r"^\s*[-\u2013_,]+\s+")  # en dash too
_TRAILING_SEP = re.compile(r"\s+[-\u2013_,]+\s*$")
_SPACES = re.compile(r"\s+")


class TemplateError(ValueError):
    pass


def sanitize_segment(name: str, max_chars: int = MAX_SEGMENT_CHARS) -> str:
    """Make one path component safe on Windows, macOS and Linux."""
    text = unicodedata.normalize("NFC", name)
    text = "".join(_REPLACEMENTS.get(ch, ch) for ch in text)
    text = _CONTROL.sub("", _SPACES.sub(" ", text))  # tabs/newlines -> spaces
    text = _SPACES.sub(" ", text).strip()
    text = text.lstrip(".").rstrip(". ")
    if len(text) > max_chars:
        text = text[:max_chars].rstrip(". ")
    while len(text.encode("utf-8")) > MAX_SEGMENT_BYTES:
        text = text[:-1].rstrip(". ")
    if not text:
        text = "_"
    if text.split(".")[0].strip().upper() in _RESERVED:
        text = "_" + text
    return text


def validate_template(template: str) -> None:
    for _, field, _, _ in string.Formatter().parse(template):
        if field is None:
            continue
        if field not in TEMPLATE_FIELDS:
            known = ", ".join(sorted(TEMPLATE_FIELDS))
            raise TemplateError(f"Unknown template field {{{field}}}. Known fields: {known}")
    if not template.rstrip().endswith(".{ext}"):
        raise TemplateError("Template must end with .{ext}")


class _Formatter(string.Formatter):
    def get_field(self, field_name: str, args: Sequence[Any], kwargs: Mapping[str, Any]) -> Any:
        # Plain names only: no "{title.__class__}" / "{title[0]}" lookups.
        if field_name not in TEMPLATE_FIELDS:
            raise TemplateError(f"Unknown template field {{{field_name}}}")
        return self.get_value(field_name, args, kwargs), field_name

    def get_value(self, key: int | str, args: Sequence[Any], kwargs: Mapping[str, Any]) -> Any:
        if isinstance(key, int):
            raise TemplateError("Positional fields are not allowed in templates")
        if key not in TEMPLATE_FIELDS:
            raise TemplateError(f"Unknown template field {{{key}}}")
        return kwargs.get(key)

    def format_field(self, value: Any, format_spec: str) -> str:
        if value is None or value == "":
            return ""
        try:
            return str(super().format_field(value, format_spec))
        except (ValueError, TypeError):
            return str(value)


_FORMATTER = _Formatter()


def template_fields(
    track: Track, ext: str, playlist: str | None = None, position: int | None = None
) -> dict[str, Any]:
    return {
        "title": track.title,
        "artist": track.primary_artist,
        "artists": ", ".join(track.artists) or None,
        "album": track.album or "Unknown Album",
        "album_artist": track.album_artist or track.primary_artist,
        "year": track.year,
        "date": track.release_date,
        "track_number": track.track_number,
        "disc_number": track.disc_number,
        "total_tracks": track.total_tracks,
        "playlist": playlist,
        "position": position,
        "isrc": track.isrc,
        "ext": ext,
    }


def _tidy(text: str) -> str:
    text = _EMPTY_BRACKETS.sub("", text)
    text = _SPACES.sub(" ", text)
    text = _LEADING_SEP.sub("", text)
    text = _TRAILING_SEP.sub("", text)
    return text.strip()


def render_path(template: str, fields: Mapping[str, Any], root: Path) -> Path:
    """Render ``template`` into a safe path under ``root``."""
    ext = str(fields.get("ext") or "")
    segments = [s for s in re.split(r"[\\/]", template) if s.strip()]
    if not segments:
        raise TemplateError("Empty template")
    rendered: list[str] = []
    for index, segment in enumerate(segments):
        is_last = index == len(segments) - 1
        text = _FORMATTER.format(segment, **fields)
        if is_last and ext and text.endswith("." + ext):
            stem = _tidy(text[: -len(ext) - 1]) or "Unknown"
            room = MAX_SEGMENT_CHARS - len(ext) - 1
            rendered.append(f"{sanitize_segment(stem, room)}.{ext}")
        else:
            rendered.append(sanitize_segment(_tidy(text) or "Unknown"))
    budget = MAX_PATH_CHARS - len(str(root)) - 1
    return root.joinpath(*_fit(rendered, ext, budget))


MIN_SEGMENT_CHARS = 16


def _fit(segments: list[str], ext: str, budget: int) -> list[str]:
    """Shorten segments until ``a/b/c.ext`` fits in ``budget`` chars: the longest
    folder names go first, the file name last (so titles survive)."""
    segments = list(segments)
    last = len(segments) - 1
    suffix = f".{ext}" if ext and segments[last].endswith(f".{ext}") else ""
    while True:
        total = sum(len(s) for s in segments) + len(segments) - 1
        over = total - budget
        if over <= 0:
            return segments
        stems = [s[: len(s) - len(suffix)] if i == last else s for i, s in enumerate(segments)]
        folders = [(len(stems[i]), i) for i in range(last) if len(stems[i]) > MIN_SEGMENT_CHARS]
        if folders:
            index = max(folders)[1]
        elif len(stems[last]) > MIN_SEGMENT_CHARS:
            index = last
        else:
            return segments  # can't shorten further; let the OS decide
        keep = max(MIN_SEGMENT_CHARS, len(stems[index]) - over)
        shortened = sanitize_segment(stems[index][:keep])
        segments[index] = shortened + suffix if index == last else shortened


def unique_path(path: Path) -> Path:
    """``song.mp3`` -> ``song (2).mp3`` (etc.) if the path is taken."""
    if not path.exists():
        return path
    for n in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Too many files named like {path}")


def claim_path(path: Path) -> Path:
    """Atomically reserve ``path`` (or ``path (2)``, ...) by creating an empty file there.

    Unlike :func:`unique_path`, two threads can never get the same name: the file is
    created with ``O_EXCL``, so the second one moves on to the next free name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    names = [path] + [path.with_name(f"{path.stem} ({n}){path.suffix}") for n in range(2, 1000)]
    for candidate in names:
        try:
            os.close(os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError(f"Too many files named like {path}")


def move_into_place(source: Path, target: Path) -> Path:
    """Move ``source`` to ``target`` (or a free ``target (n)`` name) without ever
    overwriting another file, even when several downloads finish at once. Returns the
    final path."""
    claimed = claim_path(target)
    try:
        try:
            source.replace(claimed)  # same drive: atomic, replaces our placeholder
        except OSError:
            shutil.move(str(source), str(claimed))  # another drive: copy over the placeholder
    except BaseException:
        with contextlib.suppress(OSError):
            claimed.unlink()
        raise
    return claimed
