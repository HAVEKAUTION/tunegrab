# Changelog

All notable changes to TuneGrab. Versions follow [semver](https://semver.org).

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
