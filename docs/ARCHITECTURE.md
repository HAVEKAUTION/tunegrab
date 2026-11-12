# Architecture

## Data flow

```
 URL / query
     │
     ▼
┌────────────┐   Track list + metadata   ┌───────────┐  best video id  ┌────────────┐
│  Resolver  │ ────────────────────────▶ │  Matcher  │ ──────────────▶ │ Downloader │
│ (Spotify)  │                           │ (YT Music)│                 │  (yt-dlp)  │
└────────────┘                           └───────────┘                 └─────┬──────┘
                                                                             │ raw audio
                                                                             ▼
┌────────────┐   record result   ┌──────────┐   tagged file   ┌────────────────────────┐
│  Archive   │ ◀──────────────── │  Tagger  │ ◀────────────── │ Converter (ffmpeg)     │
│ (SQLite)   │                   │(mutagen) │                 │ mp3 / m4a / opus / flac│
└────────────┘                   └──────────┘                 └────────────────────────┘
```

A **Job** wraps one user request (e.g. one playlist URL). It expands into many **TrackTasks**,
which run through the pipeline concurrently (default 4 at once), each with its own status.

## Package layout

```
tunegrab/
├── pyproject.toml
├── .env.example
├── docs/
├── src/tunegrab/
│   ├── __init__.py
│   ├── cli.py              # Typer app: download/get/search/info/match/sync/fix/retry/status/config/login/serve/doctor
│   ├── services.py         # wires settings -> http, archive, resolver, matcher, pipeline
│   ├── doctor.py           # environment checks + yt-dlp update
│   ├── logs.py             # rotating file log + --verbose
│   ├── ui.py               # Rich progress/tables
│   ├── config.py           # Settings (pydantic-settings): paths, format, bitrate, template
│   ├── net.py              # shared httpx client, retries, rate limiting
│   ├── models.py           # Track, Album, Playlist, Artist, Match, TrackTask, Job
│   ├── urls.py             # parse Spotify URLs/URIs → (kind, id)
│   ├── metadata/
│   │   ├── base.py         # MetadataProvider protocol
│   │   ├── spotify_api.py  # official Web API (client credentials + PKCE user auth)
│   │   ├── spotify_embed.py# fallback: parse public embed pages
│   │   └── resolver.py     # URL -> Collection, provider fallback + enrichment
│   ├── matching/
│   │   ├── base.py         # AudioProvider protocol: search(track) -> list[Candidate]
│   │   ├── ytmusic.py      # ytmusicapi search (songs first, then videos)
│   │   ├── youtube.py      # plain YouTube search fallback via yt-dlp "ytsearch"
│   │   ├── scoring.py      # rapidfuzz + duration + ISRC + penalties
│   │   └── matcher.py      # search order, early stop, match cache
│   ├── download/
│   │   ├── ytdlp.py        # yt-dlp as library, progress hooks, format selection
│   │   └── convert.py      # ffmpeg post-processing
│   ├── tagging/
│   │   ├── tagger.py       # mutagen: ID3 / MP4 / Vorbis tags + cover art
│   │   └── lyrics.py       # LRCLIB lookup (synced + plain)
│   ├── library/
│   │   ├── paths.py        # filename templates + sanitisation
│   │   ├── archive.py      # SQLite: downloaded tracks, match cache, failures
│   │   └── playlists.py    # write .m3u8 files
│   ├── pipeline.py         # orchestrates a Job: async queue, concurrency, retries
│   └── web/                # app.py (FastAPI, JobManager, SSE) + static/index.html
└── tests/
    ├── fixtures/           # saved JSON responses for mocks
    └── ...
```

## Core models (pydantic)

```python
class Track(BaseModel):
    spotify_id: str | None
    title: str
    artists: list[str]  # first = primary
    album: str | None
    album_artist: str | None
    track_number: int | None
    disc_number: int | None
    total_tracks: int | None
    release_date: str | None  # "YYYY" or "YYYY-MM-DD"
    duration_ms: int
    isrc: str | None
    explicit: bool = False
    cover_url: str | None
    genres: list[str] = []


class Candidate(BaseModel):
    video_id: str
    title: str
    artists: list[str]
    duration_s: int
    source: Literal["ytmusic_song", "ytmusic_video", "youtube"]
    isrc: str | None = None


class Match(BaseModel):
    track: Track
    candidate: Candidate | None
    score: float  # 0–100
    reason: str  # human-readable why


class TaskStatus(StrEnum):
    queued, resolving, matching, downloading, converting, tagging, done, skipped, failed
```

## Matching (the hard part)

Score each candidate 0–100, take the best above a threshold (default 70):

| Signal | Weight / rule |
|---|---|
| ISRC exact match (when both sides have it) | instant accept (score 100) |
| Title similarity (`rapidfuzz.token_set_ratio` on normalised titles) | 40% |
| Artist similarity (best overlap of artist sets) | 30% |
| Duration difference | 30%: full marks ≤ 3 s, linear down to 0 at ≥ 20 s |
| Source bonus | +5 for YouTube Music "song" results (official audio) |
| Penalties | −30 if the candidate has a version word the Spotify title doesn't (`live`, `cover`, `remix`, `sped up`, `slowed`, `nightcore`, `karaoke`, `instrumental`, `8d`, `re-recorded`, `vinyl`, `tab`/`lesson`/`play along`, `piano`/`guitar`/`drum`, `reaction`, `mashup`, `lofi`, `made famous by`, `in the style of`, `tribute`, ...) |
| Wrong artist | −20 when artist similarity < 50; an artist name that is a subset ("Prince" vs "Prince & The Revolution") scores 85; an artist only named in the title scores 70 (videos only) |

Normalisation: lowercase, strip `(feat. …)`, `- Remastered 2011`, `[Official Video]`,
punctuation, accents (`unicodedata`). Search order: YT Music songs → YT Music videos →
plain YouTube. Cache final matches in SQLite by Spotify ID so re-runs are instant, and let
users override a bad match (`tunegrab fix <spotify-url> <youtube-url>`).

## Output defaults

- Format: `mp3` 320k (options: `m4a` copy-no-reencode — best quality from YouTube, `opus`, `flac`*)
- Template: `{album_artist}/{album} ({year})/{track_number:02d} - {title}.{ext}`
- Playlist template: `Playlists/{playlist}/{position:03d} - {artist} - {title}.{ext}` or `.m3u8` pointing into the library (default — avoids duplicate files)
- Skip if already in archive and file still exists.

*YouTube audio is lossy (~128–160 kbps Opus/AAC). Re-encoding to 320k MP3 or FLAC
doesn't add quality; `m4a`/`opus` without re-encode is the most faithful. Say so in `--help`.

## Error handling
- Every task failure is recorded (`archive.failures`) with the reason; the job continues.
- `tunegrab retry` re-runs failed tasks. End-of-job summary table: done / skipped / failed.
- Network: 3 retries with exponential backoff; honour `Retry-After` on 429.
