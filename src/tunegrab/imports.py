"""Song lists from CSV files, e.g. playlists or Liked Songs exported with Exportify
(https://exportify.app).

A row with a title and artist becomes a track straight away (no Spotify request), so
exports of Liked Songs work without logging in to Spotify. A row with only a Spotify
track link / URI / ID is looked up through the resolver.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from collections.abc import Callable, Iterable

from tunegrab.metadata.base import MetadataError
from tunegrab.models import Collection, Track

log = logging.getLogger(__name__)

MAX_ROWS = 20_000
_BOM = chr(0xFEFF)

# Accepted header names per field (compared lowercased, without surrounding spaces).
COLUMNS: dict[str, tuple[str, ...]] = {
    "uri": ("track uri", "spotify uri", "spotify track uri", "uri", "track id", "spotify id"),
    "title": ("track name", "title", "name", "song", "song name", "track"),
    "artists": ("artist name(s)", "artist names", "artists", "artist", "artist name"),
    "album": ("album name", "album"),
    "album_artist": ("album artist name(s)", "album artist names", "album artist"),
    "release_date": ("album release date", "release date", "year"),
    "track_number": ("track number", "track no", "track #"),
    "disc_number": ("disc number", "disc no"),
    "duration_ms": ("track duration (ms)", "duration (ms)", "duration_ms"),
    "isrc": ("isrc",),
    "cover_url": ("album image url", "cover url", "image url"),
    "explicit": ("explicit",),
}

_SPOTIFY_ID = re.compile(
    r"(?:spotify:track:|open\.spotify\.com/(?:intl-[\w-]+/)?track/)?([A-Za-z0-9]{22})\b"
)
_DATE = re.compile(r"^\d{4}(?:-\d{2}(?:-\d{2})?)?$")


class CsvImportError(ValueError):
    pass


def _columns(header: Iterable[str]) -> dict[str, int]:
    names = [h.strip().lower() for h in header]
    found: dict[str, int] = {}
    for field, aliases in COLUMNS.items():
        for alias in aliases:
            if alias in names:
                found[field] = names.index(alias)
                break
    return found


def _int(text: str) -> int | None:
    try:
        return int(float(text))
    except ValueError:
        return None


def _artists(text: str) -> list[str]:
    # Exportify joins several artists with ","; other tools use ";".
    sep = ";" if ";" in text else ","
    return [a.strip() for a in text.split(sep) if a.strip()]


def spotify_track_id(text: str) -> str | None:
    match = _SPOTIFY_ID.search(text.strip())
    return match.group(1) if match else None


def read_rows(text: str) -> list[Track | str]:
    """Parse CSV text into tracks (or bare Spotify track IDs still to look up), in order."""
    text = text.lstrip(_BOM)
    first_line = text.split("\n", 1)[0]
    # The header decides the delimiter (csv.Sniffer guesses wrong on ragged rows).
    delimiter = max(",;\t", key=first_line.count)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header = next(reader, None)
    if header is None:
        raise CsvImportError("The CSV file is empty.")
    cols = _columns(header)
    if "title" not in cols and "uri" not in cols:
        raise CsvImportError(
            "The CSV needs a 'Track Name' (and 'Artist Name(s)') column or a 'Track URI' "
            "column, like the files Exportify makes."
        )
    entries: list[Track | str] = []
    for number, row in enumerate(reader, start=2):
        if number > MAX_ROWS + 1:
            raise CsvImportError(f"The CSV has more than {MAX_ROWS} songs; split it up.")
        if not any(cell.strip() for cell in row):
            continue

        def cell(field: str, row: list[str] = row) -> str:
            index = cols.get(field)
            return row[index].strip() if index is not None and index < len(row) else ""

        spotify_id = spotify_track_id(cell("uri")) if cell("uri") else None
        title, artists = cell("title"), _artists(cell("artists"))
        if not title or not artists:
            if spotify_id:
                entries.append(spotify_id)
            else:
                log.info("CSV row %d has no title/artist or Spotify ID; skipped", number)
            continue
        date = cell("release_date")
        entries.append(
            Track(
                spotify_id=spotify_id,
                title=title,
                artists=artists,
                album=cell("album") or None,
                album_artist=next(iter(_artists(cell("album_artist"))), None),
                release_date=date if _DATE.match(date) else None,
                track_number=_int(cell("track_number")),
                disc_number=_int(cell("disc_number")),
                duration_ms=_int(cell("duration_ms")) or 0,
                isrc=cell("isrc").upper() or None,
                cover_url=cell("cover_url") or None,
                explicit=cell("explicit").lower() == "true",
            )
        )
    return entries


def playlist_id(name: str) -> str:
    """A stable ID for the archive's playlist table (and the .m3u8 file)."""
    return "csv:" + (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "songs")


def collection_from_csv(
    text: str, name: str, lookup: Callable[[str], Track] | None = None
) -> Collection:
    """Build a playlist from CSV text. ``lookup`` turns a Spotify track ID into a Track
    (rows without a title need it); IDs that can't be found are counted as skipped."""
    tracks: list[Track] = []
    warnings: list[str] = []
    skipped = 0
    for entry in read_rows(text):
        if isinstance(entry, Track):
            tracks.append(entry)
            continue
        if lookup is None:
            skipped += 1
            continue
        try:
            tracks.append(lookup(entry))
        except (MetadataError, LookupError, IndexError) as exc:
            log.info("CSV: could not look up %s: %s", entry, exc)
            skipped += 1
    if skipped:
        warnings.append(f"{skipped} row(s) had no title or a Spotify ID that couldn't be found.")
    if not tracks:
        raise CsvImportError("No songs found in the CSV.")
    return Collection(
        kind="playlist",
        id=playlist_id(name),
        name=name,
        tracks=tracks,
        total=len(tracks) + skipped,
        skipped=skipped,
        warnings=warnings,
        source="csv",
    )
