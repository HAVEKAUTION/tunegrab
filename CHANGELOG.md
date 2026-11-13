# Changelog

All notable changes to TuneGrab. Versions follow [semver](https://semver.org).

## Unreleased

### Added
- **Check this match.** A song TuneGrab wasn't sure about (a match score under 85, or a video
  that says "live", "remix", "cover" and the like when the song doesn't) is marked in Downloads
  and Library. Open it to see the saved video and up to three other candidates with channel,
  length, score and a Listen link. "Use this one" downloads that video and replaces the file
  only once the new one is tagged and ready; "Keep the saved one" stops the question. Either
  choice is remembered for the song.
- **Search finds albums and songs.** Type an album or song ("Discovery Daft Punk", "One More
  Time") to see matching albums and songs from YouTube Music and tick them into the queue. An
  artist's name still opens the artist.
- **Download now names the song it picked** ("One More Time – Daft Punk"); an artist's name
  opens Browse instead of quietly downloading one of their songs.
- **Videos are remembered**: video and audio-from-link downloads are in the download history,
  shown in Library → Videos with Show, skipped when downloaded again with the same options,
  and failed ones come back in "Retry failed" after a restart.
- **Show in folder** on every saved song in Downloads.
- **YouTube sign-in** in Settings (off by default): use a browser's YouTube cookies for
  age-restricted videos (and 256 kbps audio with Premium), with a clear warning that YouTube
  can restrict the account used. Age-restricted failures now point to it.
- **Update notice**: once a day TuneGrab asks GitHub and PyPI whether a newer TuneGrab or
  yt-dlp is out and shows a dismissible banner (Update yt-dlp is one click). Settings → Updates
  turns it off.

### Fixed
- **The Windows installer could delete your music.** Updating (re-running the install line)
  mirrored the release over the install folder, which deleted the default music folder
  (`downloads`) and any `.env` kept there; `-Uninstall` deleted them too. The installer now
  replaces only its own files, records them in `.tunegrab-installed`, and uninstalling leaves
  everything else in place and lists it. Because the install line always fetches the newest
  `install.ps1`, this protects existing 0.4.1 installs on their next update.
- A job can no longer hang in "running" forever: any unexpected error ends it with a plain
  message, and one broken video no longer stops a video job.
- Two songs that finish at the same moment and render to the same file name can't overwrite
  each other; the second is saved as "name (2)".
- A song listed twice in one playlist downloads once.
- A missing music folder or a full disk stops the job once with a clear message (instead of
  failing every song); Retry picks the songs up once fixed. Settings refuses a music folder
  TuneGrab can't write to.
- A broken settings file or download history no longer stops TuneGrab starting: it's set
  aside with a dated name and the app says so. Your music files are untouched.
- Stopping the server while downloading lets running songs end cleanly.
- Songs already in your library are recognised from their tags even if the download history
  was lost, instead of being downloaded again as "(2)" copies.
- `tunegrab fix` keeps your old file until the replacement is downloaded; `sync --remove`
  reports files it can't delete (e.g. open in a player); `login` network errors are a message.
- Only one "Choose folder" window can be open at a time (a second one crashed the server).
- Retry retries each failed song once, however many times it's clicked.
- The Downloads page catches up after a lost connection or server restart, stays responsive
  on 2,000-song jobs, and no longer shows a song stuck on "Downloading" after it finished.
- Messages no longer send app users to the command line or `.env`.
- Retrying a failed song (Retry, Retry failed after a restart, the link fix, video retries)
  uses the format the job was started with, not the current default.
- "Use a YouTube link" no longer appears on failed videos.
- The queue bar says "1 song", not "1 songs"; Settings fields line up when only one has help.

### Changed
- Downloads are paced to stay under YouTube's limit: about 30 back to back, then 250 an hour
  (1,000 with browser cookies). If YouTube still says "slow down" three times in a job,
  TuneGrab pauses for an hour (the app shows when it continues, with a Try now button); after
  five it stops and leaves the songs for Retry. Rate limits no longer use up a song's retries.
- A song YouTube refuses with every player client now says "YouTube may have changed
  something: update yt-dlp in Settings, then press Retry".
- **New installs save music to `~/Music/TuneGrab`** and use **M4A** (YouTube's original
  audio, no re-encode). Existing users keep their music folder and MP3: both are saved to the
  settings file the first time this version runs.
- MP3 without a chosen bitrate is now VBR V0 (about 245 kbps): sounds the same as 320 for
  YouTube audio and is smaller. A chosen bitrate stays constant.
- Format labels say what each one does: "M4A · original quality", "Opus · original quality,
  smallest", "MP3 · re-encoded, for old players".
- The download history upgrades itself (format 3) and keeps a copy of the old file
  (`tunegrab.db.bak-v<n>`) first; a newer history than this version knows is refused.
- Under the hood: one error type, a shared task runner for songs and videos, the web server
  and page split into smaller files, dead code removed.

## 0.4.1 — 2026-11-13

### Added
- One-line Windows installer (`install.ps1`): installs uv, ffmpeg and Deno if missing, the
  latest release, Start-menu and desktop shortcuts; re-run to update, `-Uninstall` to remove.
- New logo (a music note), used in the app, its tab icon and the shortcuts.
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
