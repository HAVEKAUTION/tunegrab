# Changelog

All notable changes to TuneGrab. Versions follow [semver](https://semver.org).

## 0.4.1 — 2026-11-13

### Added
- One-line Windows installer (`install.ps1`): installs uv, ffmpeg and Deno if missing, the
  latest release, Start-menu and desktop shortcuts; re-run to update, `-Uninstall` to remove.
- New logo (a music note shaped like a capital T), used in the app, its tab icon and the shortcuts.
- README: demo GIF, project status.

### Fixed
- The "Save as" label on the video card sat lower than the other labels.

## 0.4.0 — 2026-11-13

Videos and clips, more dependable YouTube downloads, and a smaller footprint. Built from a
read through yt-dlp's issue tracker. No new dependencies.

### Added
- **Videos and clips**: paste a YouTube (or any yt-dlp-supported site) link into
  **Get music** to save the video as MP4 (best up to 4K; 1080p by default), only its audio,
  or a clip between two times. Playlists download every video. Also `tunegrab video`.
  Clips are cut by TuneGrab's own ffmpeg step, frame-accurate (avoids yt-dlp#16546).
- **Settings → Video** (quality) and `video_dir`; videos go to `<music folder>/Videos`.
- **Settings → System → Update yt-dlp**, and a warning when yt-dlp is over 60 days old.
- **Settings → Storage** and `tunegrab clean`: see what TuneGrab keeps on disk and free it
  (unfinished downloads, old logs, yt-dlp's cache, database compaction, optionally uv's cache).
- Each finished song shows the source it was made from (e.g. *Opus 129 kbps*).
- README: disk space and uninstall guide.

### Changed
- YouTube's "stable volume" (DRC) audio is never chosen when the normal stream exists
  (yt-dlp#8259); AI-upscaled "super resolution" video is skipped (yt-dlp#15433).
- Every JavaScript runtime yt-dlp supports is enabled (Deno, Node.js 22+, Bun, QuickJS),
  not only Deno, whose development is winding down (yt-dlp#17799). `doctor` accepts any.
- When YouTube answers 403 / "unplayable" / only a low-quality format, the download is
  retried as other YouTube players before giving up (yt-dlp#17989, #17389, #17542).
  A genuinely low-quality upload is accepted straight away.
- A video that's unavailable (removed, private, region-blocked, age-gated) is replaced by
  the next-best match instead of failing the song.
- "Too many requests" / "confirm you're not a bot" pauses all downloads (60 s, doubling to
  5 min) instead of failing songs one after another.
- The launcher installs and runs without the developer tools (`--no-dev`): ~60 MB less.
- The empty `.tunegrab-tmp` folder is removed from the music folder after each job.
- yt-dlp's disk cache is off: measured, it saved no time per song.
- Updating yt-dlp only touches yt-dlp's packages, never TuneGrab's own launcher file
  (which Windows locks while the app runs).

## 0.3.0 — 2026-11-13

Ideas and bug reports from other downloaders' issue trackers, checked against TuneGrab.

### Added
- **Use a YouTube link** for a failed song in Downloads: paste the right video, TuneGrab
  downloads it and remembers it (like `tunegrab fix`, without the terminal).
- **CSV import** of playlists or Liked Songs (Exportify exports or any list with track name
  and artist columns) in the app and with `tunegrab download --from-file list.csv`. Songs
  are read straight from the file, so no Spotify login or API calls are needed.

### Changed
- Matching penalises extended, acoustic, unplugged and demo versions you didn't ask for.
- Official music videos up to 90 s longer than the song (intros/outros) keep half the
  duration points instead of none.
- Only Latin accents are folded when comparing titles; Japanese voiced marks and other
  scripts' letters are kept, so different titles no longer look identical.

### Fixed
- A saved Spotify login made with a different client ID is ignored instead of being used
  until it expires.

### Security
- File-name templates only accept plain fields (`{title}`, not `{title.__class__}` or
  `{title[0]}`) and must end in `.{ext}`.

## 0.2.0 — 2026-11-12

### Added
- Browse an artist's releases and tick whole albums or single songs into a download queue
  that spans artists and survives closing the page.
- **Full discography** from YouTube Music when Spotify's public page lists only the newest releases.
- Redesigned web app: sidebar navigation, cover grid, song picker panel, queue bar with
  review dialog, toasts with **Undo**, keyboard shortcuts (`/`, `1`–`4`, `Ctrl+Enter`, `?`).
- **System** panel and banner showing whether ffmpeg, Deno and yt-dlp are ready.
- **About** panel with version and the locations of the music folder, log and settings files.
- Download history that survives restarts, **Clear finished**, and **Retry failed songs**
  across restarts.
- Settings: folder picker, **Reset to defaults** with Undo, unsaved-changes indicator.
- Library shows cover art and flags files that were moved or deleted.

### Changed
- `TuneGrab.bat` checks its environment and rebuilds it if the folder was moved, then starts
  the app; the browser opens only once the server answers.
- `tunegrab serve` reuses an already-running TuneGrab instead of failing on a busy port.

### Security
- The local server rejects requests with a foreign `Host` header (DNS rebinding) and
  cross-site `POST`/`DELETE` requests.

## 0.1.0 — 2026-11-11

First working version: Spotify metadata (API or public pages), YouTube Music / YouTube
matching with scoring, yt-dlp download, ffmpeg conversion, tags, cover art, lyrics,
SQLite archive, playlists, sync, retry, fix, CLI and a basic web UI.
