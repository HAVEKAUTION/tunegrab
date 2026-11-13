<div align="center">

<img src="https://raw.githubusercontent.com/HAVEKAUTION/tunegrab/main/docs/images/logo.svg" alt="" width="84">

# TuneGrab

**Pick albums, playlists and songs from Spotify. Get a tidy, fully tagged music library on your own drive.**

[![CI](https://github.com/HAVEKAUTION/tunegrab/actions/workflows/ci.yml/badge.svg)](https://github.com/HAVEKAUTION/tunegrab/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/tunegrab)](https://pypi.org/project/tunegrab/)
![Platforms](https://img.shields.io/badge/runs%20on-Windows%20%7C%20macOS%20%7C%20Linux-555)
![License](https://img.shields.io/badge/license-GPL--3.0-blue)

<img src="https://raw.githubusercontent.com/HAVEKAUTION/tunegrab/main/docs/images/demo.gif" alt="Typing an artist into TuneGrab, ticking two albums into the queue, then pasting a YouTube link to download a clip" width="900">

**[Get started](#get-started)** · **[How to use it](#how-to-use-it)** · **[Jellyfin / Plex](#use-with-jellyfin-navidrome-or-plex)** · **[FAQ](#faq)** · **[Troubleshooting](#troubleshooting)** · **[Advanced](#advanced)**

</div>

## What it does

- **Browse, then download.** Type an artist or paste a Spotify link, tick the albums or songs you want.
- **Tagged files, sorted for you.** `Artist/Album (Year)/01 - Title.m4a`, with cover art and lyrics.
- **Picks the right version.** Skips live, cover and sped-up uploads, and flags any match it wasn't sure about so you can check it.
- **Never twice.** It remembers what you have; re-running a playlist only fetches what's new.
- **Videos too.** Paste a YouTube link to save the video, a clip of it, or just the audio.
- **Yours only.** Runs on your computer, no account needed. Spotify is only read for track
  details; the audio comes from YouTube.

## Get started

**Windows:** open PowerShell and paste:

```powershell
irm https://raw.githubusercontent.com/HAVEKAUTION/tunegrab/main/install.ps1 | iex
```

It installs what it needs, adds TuneGrab to the Start menu and opens it. Run the same line
again to update.

**macOS / Linux:** install [uv](https://docs.astral.sh/uv/), ffmpeg and Deno, then:

```bash
uv tool install tunegrab
tunegrab serve            # opens http://127.0.0.1:8765 in your browser
```

Then: **type an artist → tick what you want → Download.** That's it.

## How to use it

<img src="https://raw.githubusercontent.com/HAVEKAUTION/tunegrab/main/docs/images/get-music.jpg" alt="The Get music start screen with a search box and example artists" width="900">

<details>
<summary><b>Download albums from an artist</b></summary>

<br>

1. On **Get music**, type the artist's name (or paste their Spotify link) and press **Enter**.
2. Click **Full discography** if you want more than the newest releases.
3. Click the covers you want; a green tick shows they're queued. Use **Choose songs** to pick
   individual tracks from a release.
4. Repeat for as many artists as you like. The queue bar at the bottom keeps a running total.
5. Press **Download** (or `Ctrl+Enter`) and watch progress on **Downloads**.

<img src="https://raw.githubusercontent.com/HAVEKAUTION/tunegrab/main/docs/images/choose-songs.jpg" alt="The Choose songs panel with two tracks of an album ticked" width="800">

</details>

<details>
<summary><b>Download a playlist, album or single song</b></summary>

<br>

Paste the Spotify link into **Get music** and press **Download now** to skip the preview, or
**Browse** to see and pick the tracks first. For one song you can also type its title
(or `Artist - Title`) and press **Browse** to pick it from the results, or **Download now**.

Playlists also get an `.m3u8` playlist file. **Check for new songs** in **Library** later
downloads only what was added.

</details>

<details>
<summary><b>Download your Liked Songs (or any playlist) from a CSV</b></summary>

<br>

1. Go to [exportify.app](https://exportify.app), log in with Spotify and press **Export**
   next to *Liked Songs* (or any playlist). You get a `.csv` file.
2. In TuneGrab, under the search box on **Get music**, click **Import a CSV file** and pick it.
3. Everything downloads as one playlist named after the file, with its own `.m3u8`.

Any CSV with **Track Name** and **Artist Name(s)** columns works (commas, semicolons or tabs),
and so does a single **Track URI** column of Spotify links.

</details>

<details>
<summary><b>Download a video, a clip of one, or just its audio</b></summary>

<br>

1. Paste a YouTube link (or one from any
   [site yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md))
   into **Get music** and press **Browse**.
2. Pick **Video (MP4)** and a quality (up to 4K), or **Audio only**. For a clip, fill in
   **Clip from** and **to** (`1:23`, `83` or `1:02:03`). Playlists download every video.
3. Press **Download**. Videos go to `Videos/Channel/Title [id].mp4` in your music folder.

**Download now** on a video link saves just its audio into your music folder.

<img src="https://raw.githubusercontent.com/HAVEKAUTION/tunegrab/main/docs/images/video.jpg" alt="A YouTube video in TuneGrab with Video (MP4), 1080p and a clip from 0:43 to 1:10 selected" width="900">

</details>

<details>
<summary><b>Fix failed or wrong songs</b></summary>

<br>

- **Failed:** open **Downloads** and press **Retry failed songs**. Each failure shows its reason.
- **Not found at all:** press **Use a YouTube link** under the failed song, paste the right
  video (there's a search link right in the dialog) and press **Download this one**.
- **Wrong version downloaded:** songs TuneGrab had doubts about show **Check this match** in
  Downloads and Library. Open it to compare the other candidates, listen, and swap with one
  click; the old file is replaced only once the new one is ready. TuneGrab remembers the fix.

<img src="https://raw.githubusercontent.com/HAVEKAUTION/tunegrab/main/docs/images/downloads.jpg" alt="The Downloads screen with an album half downloaded, showing match scores and per-song status" width="800">

</details>

<details>
<summary><b>Change where music is saved, the format, or file names</b></summary>

<br>

Music is saved to **Music\TuneGrab** in your user folder unless you pick another one.
(If you used TuneGrab 0.4 or older, it keeps using the `downloads` folder it used before.)
**Settings → Music folder → Choose…** opens a folder picker. Press **Save**. File naming,
format, quality, lyrics and how many songs download at once are on the same screen.

<img src="https://raw.githubusercontent.com/HAVEKAUTION/tunegrab/main/docs/images/settings.jpg" alt="The Settings screen with music folder, audio format and download options" width="800">

</details>

<details>
<summary><b>Your library</b></summary>

<br>

- Everything you've downloaded, grouped by artist and album, with covers, and an instant
  filter (fast even with thousands of songs).
- **Show** opens a song's folder; files you've moved or deleted are flagged.
- Each song shows what YouTube actually served ("MP3 · from Opus 129 kbps") and is marked
  when only a low-quality source existed.
- **Check for new songs** on a playlist downloads what was added and lists what left it.
  Files of songs that left are deleted only if you tick them and confirm a list of every
  file; songs another playlist still has are never deleted.

<img src="https://raw.githubusercontent.com/HAVEKAUTION/tunegrab/main/docs/images/library.jpg" alt="The Library screen listing an album with its cover art" width="800">

</details>

<details>
<summary><b>Keyboard shortcuts</b></summary>

<br>

| Key | Does |
|---|---|
| `/` | Jump to search |
| `1` `2` `3` `4` | Get music · Downloads · Library · Settings |
| `Space` | Tick the focused cover or song |
| `Ctrl+Enter` | Download the queue |
| `Esc` | Close a panel or dialog |
| `?` | Show all shortcuts |

</details>

## Use with Jellyfin, Navidrome or Plex

TuneGrab's music folder is already laid out the way media servers expect, so there's nothing
to convert: **add TuneGrab's music folder as a music library** in your server and scan it.

- **Folders:** `Album Artist/Album (Year)/01 - Title.m4a`.
- **Tags:** title, artists, album artist, album, track and disc number, year, genre and ISRC,
  with cover art and lyrics embedded in every file.
- **Synced lyrics:** turn on **Settings → Downloads → Also save synced lyrics (.lrc)**.
  Jellyfin and Navidrome show them scrolling along with the song.
- **Playlists:** each playlist gets an `.m3u8` file in `Playlists/`, with paths relative to
  the file, so it still works when the server sees the folder under a different path.
  Navidrome imports them automatically; other servers can import the file.
- **Videos:** these land in `Videos/` inside the music folder. To keep them out of your music
  library, move them elsewhere with `tunegrab config set video_dir <folder>`, or add
  `Videos/` to your server as a separate library.

Server in Docker? Mount the folder read-only, for example:

```yaml
volumes:
  - /path/to/Music/TuneGrab:/music:ro
```

Lidarr users can add the same folder as a root folder; Lidarr recognises most albums from
the tags.

## FAQ

<details>
<summary><b>Do I need a Spotify account or Premium?</b></summary>

<br>

No. Public links and artist names work with no account. Premium is only needed if you
want to create your own Spotify developer app (see [Advanced](#advanced)).

</details>

<details>
<summary><b>Can I download my Liked Songs?</b></summary>

<br>

Yes. Export them with [Exportify](https://exportify.app) and use **Import a CSV file** on
**Get music**. TuneGrab reads the song details straight from the file, so it doesn't need
your Spotify login.

</details>

<details>
<summary><b>What audio quality do I get?</b></summary>

<br>

YouTube audio is lossy (typically around 128–160 kbps Opus/AAC; 256 kbps with YouTube Premium
and **Settings → YouTube sign-in**). **M4A** (the default) and **Opus** keep the original
stream untouched; M4A plays on phones, cars and Windows, Opus makes the smallest files.
**MP3** and **FLAC** re-encode it for old players, but can't add quality that isn't there.
MP3 uses VBR V0 unless you pick a bitrate: it sounds the same as 320 kbps for YouTube audio
and is smaller. If you used TuneGrab before 0.5, it stays on MP3 until you change it.

TuneGrab skips YouTube's volume-levelled "stable volume" copy, retries when YouTube only
offers a low-quality one, and flags any song that had to settle for it.

</details>

<details>
<summary><b>What video quality do I get?</b></summary>

<br>

The best available up to the quality you pick (1080p by default), as H.264 + AAC MP4 that
plays anywhere. Above 1080p YouTube only offers VP9/AV1. AI-upscaled copies are skipped.

</details>

<details>
<summary><b>How does it pick the right song on YouTube?</b></summary>

<br>

- Searches YouTube Music first (official audio), then regular YouTube.
- Scores every candidate (0–100) on title, artist, duration and album, and penalises live,
  cover, karaoke, sped-up, remix, extended, acoustic and demo versions you didn't ask for.
- Official music videos aren't thrown out just because their intro makes them longer.
- Titles are compared as written in any script (Japanese, Korean, Cyrillic, ...); only
  Latin accents are folded (Beyoncé = Beyonce).
- Songs that score below the threshold (default 70) are flagged instead of downloaded wrongly.
- A match it isn't sure about (score under 85, or a "live" / "remix" / "cover" video for a
  plain song) is downloaded but marked **Check this match**.

</details>

<details>
<summary><b>Will it download the same song twice?</b></summary>

<br>

No. Every download is recorded in a small database, and anything already in your library is
skipped. Re-running a big job only fetches what's missing, so it's safe to stop and start again.

</details>

<details>
<summary><b>Can I close the browser tab?</b></summary>

<br>

Yes. Downloads keep running as long as the launcher window is open. Reopen
`http://127.0.0.1:8765` (or double-click the launcher again) to check progress. The queue is
kept too, even across a restart.

</details>

<details>
<summary><b>What data leaves my computer?</b></summary>

<br>

Only requests to Spotify (track information), YouTube / YouTube Music (search and audio),
LRCLIB (lyrics) and the image servers for cover art. Once a day TuneGrab also asks GitHub
and PyPI for the newest TuneGrab and yt-dlp version numbers (turn it off in
**Settings → Updates**); nothing about you or your music is sent. There's no tracking or
crash reporting. Logs stay on your PC (**Settings → About** shows where).

**Who can use the app on my PC?** It listens only on `127.0.0.1`, so websites and other
computers can't reach it. It has no password, though: any program running on your computer
(and any other user signed in to it) can talk to it. That's normal for a personal tool;
don't run it with `--host 0.0.0.0` on a shared network.

**Is the installer safe?** Each release carries a `SHA256SUMS` file, and the installer
refuses a download whose checksum doesn't match.

</details>

<details>
<summary><b>Is this legal?</b></summary>

<br>

It depends where you live. In Australia, downloading copyrighted music from YouTube without
permission isn't covered by the format-shifting exception (which only applies to recordings
you already legitimately own), and it's against YouTube's terms. Lots of people do it for
personal use, but it's your call. The same goes for videos: only download ones you're
allowed to. TuneGrab deliberately **does not** decrypt Spotify audio.

</details>

<details>
<summary><b>Isn't there already a tool like this?</b></summary>

<br>

Yes: [spotDL](https://github.com/spotDL/spotify-downloader) is free, open source and works the
same way. TuneGrab focuses on a visual "browse, tick and queue" app, and on picking a whole
discography across several artists in one go.

</details>

## Troubleshooting

<details>
<summary><b>YouTube downloads suddenly all fail</b></summary>

<br>

YouTube changes often and yt-dlp keeps up. In the app: **Settings → System → Update
yt-dlp**, then restart TuneGrab. Or from a terminal: `tunegrab doctor --update`
(`uv tool upgrade tunegrab` if you installed it that way).

Also check Deno is installed (`deno --version`).

</details>

<details>
<summary><b>"Sign in to confirm you're not a bot"</b></summary>

<br>

YouTube limits how many videos one connection can fetch (roughly 300 an hour without
signing in). TuneGrab stays under that: big jobs start at full speed, then slow to about 250
songs an hour. If YouTube still says "slow down", TuneGrab waits a minute, then two; the
third time in a job it pauses for an hour (YouTube's block lasts about that long) and shows
when it will continue, with a **Try now** button. After five it stops and leaves the songs
for **Retry**.

If it keeps happening, turn off any VPN. Signing in raises the limit, but YouTube can
restrict the Google account used, so only do it with an account you can afford to lose
access to: in **Settings → YouTube sign-in**, choose the browser you use YouTube in (Firefox
works best; on Windows, Chromium browsers usually have to be closed and may not be readable).
The same setting lets you download age-restricted videos.

</details>

<details>
<summary><b>A song matched the wrong version</b></summary>

<br>

If the song shows **Check this match** (Downloads or Library), open it and press **Use this
one** on the right video. Otherwise, from a terminal:

```powershell
tunegrab match <spotify-link>                       # see the scores
tunegrab fix <spotify-track-link> <youtube-link>    # force the right one
```

</details>

<details>
<summary><b>A playlist stops at 100 songs</b></summary>

<br>

That's the limit of Spotify's public pages. Either export the playlist as a CSV (see
**How to use it**) or add Spotify developer credentials (see [Advanced](#advanced)).

</details>

<details>
<summary><b>The launcher closes straight away or says "uv" is missing</b></summary>

<br>

Install uv (`winget install astral-sh.uv`), then reopen the launcher. If you moved the
TuneGrab folder, the launcher rebuilds its environment automatically; just wait for
"Setting up TuneGrab...". Keep the black launcher window open while you use TuneGrab:
closing it stops TuneGrab.

</details>

<details>
<summary><b>"Port 8765 is already used by another program"</b></summary>

<br>

Something else is using that port. Start on another one: `tunegrab serve --port 8766`.

</details>

<details>
<summary><b>Where are the logs?</b></summary>

<br>

**Settings → About** shows the log file path with a copy button. Run any command with `-v` to
also see details in the terminal. Check everything is installed with **Settings → System**
(every row should be green) or `tunegrab doctor`.

</details>

---

## Advanced

Command line, config, installing by hand and working on the code.
Commands below assume `tunegrab` is on your PATH; in a clone, put `uv run` in front.

<details>
<summary><b>How it works</b></summary>

<br>

```
 Spotify link or artist name
          │
          ▼
 ① Resolve   read the track list and details from Spotify (metadata only)
 ② Match     find each song on YouTube Music / YouTube and score the candidates
 ③ Download  fetch the audio with yt-dlp
 ④ Convert   M4A (original audio), Opus, MP3 or FLAC with ffmpeg
 ⑤ Tag       title, artists, album, track no., year, ISRC, cover art, lyrics
 ⑥ Archive   remember it, so it's never downloaded twice
          │
          ▼
 Artist/Album (Year)/01 - Title.m4a
```

Spotify is used **only** for track information; TuneGrab never touches Spotify's own audio
streams. Spotify's public artist page lists only the newest 8 albums and 8 singles, so
**Full discography** loads the complete list from YouTube Music. Several songs download at
once, unavailable videos fall back to the next-best match, and a YouTube "slow down" pauses
the queue instead of failing it.

</details>

<details>
<summary><b>Command line</b></summary>

<br>

Everything in the app is also available from a terminal.

```powershell
tunegrab get "Daft Punk - One More Time"                        # one song, best match
tunegrab download https://open.spotify.com/album/2noRn2Aes5aoNVsU6iWThc
tunegrab download <playlist-url> -f m4a -o D:\Music             # choose format and folder
tunegrab download --from-file my-links.txt                      # one link per line
tunegrab download --from-file Liked_Songs.csv                   # an Exportify CSV
tunegrab video https://youtu.be/dQw4w9WgXcQ                     # the video, as MP4 (1080p)
tunegrab video <link> --start 0:43 --end 1:10 -q 720            # just a clip
tunegrab video <link> --audio -f opus                           # only the audio
tunegrab serve                                                  # start the app
```

| Command | What it does |
|---|---|
| `download <url-or-query>... [--from-file list.txt]` | The main command. Accepts several inputs; `--from-file` reads one per line (`#` comments OK), or a `.csv` export (Exportify etc.). Options: `-f/--format mp3\|m4a\|opus\|flac`, `-b/--bitrate`, `-o/--out`, `-t/--threads`, `--lyrics/--no-lyrics`, `--lrc` (synced `.lrc` file), `--playlist-mode m3u\|folder`. |
| `get "artist - title"` | Download the top search result. |
| `video <link>` | Download a video (MP4) from YouTube or another yt-dlp site, or a playlist of them. `--audio` for only the audio (`-f` format), `-q/--quality best\|2160\|1440\|1080\|720\|480\|360`, `-s/--start` and `-e/--end` for a clip, `-o/--out` for the folder. |
| `search "query"` | Show results and pick one interactively. |
| `info <url>` | List the tracks behind a link without downloading. |
| `match <url> [--threshold N] [--no-cache]` | Dry run: which YouTube video each track would use, its score and why. |
| `sync <playlist-url> [--remove] [--yes]` | Download new tracks in a playlist. Lists tracks that left it; `--remove` deletes them (asks first, keeps files used by another playlist). |
| `fix <spotify-track-url> <youtube-url-or-id>` | Override a wrong match, remember it and re-download (`--no-download` to only save the override). |
| `retry [--clear]` | Re-run every track that failed before, or forget them with `--clear`. |
| `status` | Library and archive statistics, plus open failures. |
| `config show \| set <key> <value> \| unset <key> \| path` | View or change settings (`set <key> none` resets one). |
| `login` / `logout` | Optional Spotify login for private playlists and Liked Songs. |
| `serve [--host] [--port] [--no-open]` | Start the app on `127.0.0.1:8765`. |
| `doctor [--update]` | Check everything is installed; `--update` upgrades yt-dlp. |
| `clean [--dry-run] [--uv-cache]` | Show what TuneGrab keeps on disk and free space (unfinished downloads, old logs, yt-dlp cache, compact the history). Never touches your music. |

Global flags: `--version`, `-v/--verbose` (logs always go to a rotating file too).

</details>

<details>
<summary><b>Settings and file naming</b></summary>

<br>

From highest to lowest priority: **command-line flags → `TUNEGRAB_*` environment variables →
`.env` (in the settings folder or the install folder) → system keyring → config file →
defaults.** Run `tunegrab config show` to see every setting and where its value came from.

| Setting | Default | Meaning |
|---|---|---|
| `output_dir` | `~/Music/TuneGrab` | Music folder (installs from 0.4.x keep their `downloads` folder) |
| `format` | `m4a` | `m4a`, `opus`, `mp3` or `flac`. Profiles from before 0.5 keep `mp3` |
| `bitrate` | — (V0) | MP3 only: unset is LAME VBR V0 (about 245 kbps); a number is constant kbps |
| `threads` | `4` | Songs downloaded at once (1–16) |
| `lyrics` / `lrc` | on / off | Embed lyrics / also write a synced `.lrc` |
| `playlist_mode` | `m3u` | `m3u` playlist file, or `folder` per playlist |
| `match_threshold` | `70` | Minimum match score (0–100) |
| `cookies_from_browser` | — (off) | `firefox`, `chrome`, `edge`, …: YouTube sign-in for age-restricted videos. YouTube can restrict the account used |
| `video_quality` | `1080` | Highest video resolution: `best`, `2160` … `360` |
| `check_updates` | `true` | Once a day, ask GitHub and PyPI whether a newer TuneGrab or yt-dlp is out |
| `video_dir` | `<music folder>/Videos` | Where videos are saved |
| `track_template` | see below | File naming pattern |

**File naming pattern.** Default: `{album_artist}/{album} ({year})/{track_number:02d} - {title}.{ext}`

Available fields: `{title} {artist} {artists} {album} {album_artist} {year} {date}
{track_number} {disc_number} {total_tracks} {playlist} {position} {isrc} {ext}`

</details>

<details>
<summary><b>Spotify developer credentials (optional)</b></summary>

<br>

Without credentials TuneGrab reads Spotify's public pages. That covers tracks, albums and
playlists, with two limits: **playlists stop at the first 100 tracks** (you get a warning
with the real count) and **artist links give their top 10 tracks** (use the app's artist
browser or **Full discography** for everything).

To lift those limits:

1. Create an app at [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
   (Development Mode needs the app owner to have **Spotify Premium**). Redirect URI:
   `http://127.0.0.1:8888/callback`.
2. Copy `.env.example` to `.env` and fill in `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`,
   or run `tunegrab config set spotify_client_secret ...` to store it in the system keyring.
3. Run `tunegrab login` once if you also want private playlists or Liked Songs.

Credentials are kept in a git-ignored `.env` file or your system keyring. TuneGrab reads
`.env` only from its settings folder and its install folder, never from the folder you happen
to run it in. Full walkthrough: [`docs/SETUP.md`](https://github.com/HAVEKAUTION/tunegrab/blob/main/docs/SETUP.md).

</details>

<details>
<summary><b>Other ways to install</b></summary>

<br>

TuneGrab needs three free tools: **uv** (runs Python for you), **ffmpeg** (audio conversion)
and **Deno** (YouTube needs it).

**What the Windows one-liner does:** installs uv, ffmpeg and Deno if they're missing (with
winget), puts the latest release in `%LOCALAPPDATA%\Programs\TuneGrab`, checks it against the
release's `SHA256SUMS`, adds TuneGrab to the Start menu and desktop, and opens it.
[Read the script](https://github.com/HAVEKAUTION/tunegrab/blob/main/install.ps1) first if you like: it's short.

**Windows by hand:**

1. `winget install astral-sh.uv Gyan.FFmpeg DenoLand.Deno`, then reopen PowerShell.
2. `git clone https://github.com/HAVEKAUTION/tunegrab.git` (or **Code → Download ZIP**).
3. Double-click **`TuneGrab.bat`**. The first start takes about a minute.

**macOS:**

```bash
brew install uv ffmpeg deno
uv tool install tunegrab
tunegrab serve
```

**Linux:**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # uv
sudo apt install ffmpeg                             # or your distro's package manager
curl -fsSL https://deno.land/install.sh | sh        # Deno
uv tool install tunegrab
tunegrab serve
```

`pip install tunegrab` works too (Python 3.11+); you still need ffmpeg and Deno. Stop
TuneGrab first if it's running when you reinstall (Windows locks the program file while it
runs).

**Check everything is ready:** **Settings → System** in the app (every row green), or
`tunegrab doctor`. It checks Python, ffmpeg, Deno (or Node.js 22+ / Bun / QuickJS), yt-dlp
(warning when it's out of date), your music folder and any Spotify credentials, and says
what to fix.

</details>

<details>
<summary><b>Updating</b></summary>

<br>

```powershell
irm https://raw.githubusercontent.com/HAVEKAUTION/tunegrab/main/install.ps1 | iex   # Windows one-liner install
uv tool upgrade tunegrab          # installed with `uv tool install`: TuneGrab and yt-dlp
git pull                          # a clone: get the latest TuneGrab
tunegrab doctor --update          # only yt-dlp (do this whenever YouTube downloads start failing)
```

Or in the app: **Settings → System → Update yt-dlp**, then restart TuneGrab. The launcher
re-syncs everything else by itself on the next start. The app tells you when a new TuneGrab
or yt-dlp is out (checked once a day; **Settings → Updates** turns it off).

</details>

<details>
<summary><b>Disk space and uninstalling</b></summary>

<br>

Besides your music, TuneGrab keeps the app folder (about 50 MB) and its settings, history
and logs in `%LOCALAPPDATA%\tunegrab` (a few MB). **Settings → Storage → Free up space**
(or `tunegrab clean`) clears leftovers; `tunegrab clean --uv-cache` also prunes uv's shared
package cache. Your music, settings and history stay.

**Uninstall** (installed with the one-liner):

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/HAVEKAUTION/tunegrab/main/install.ps1))) -Uninstall
```

Installed with uv: `uv tool uninstall tunegrab`. A clone: delete the `tunegrab` folder.
Settings and history live in `%LOCALAPPDATA%\tunegrab` (on macOS
`~/Library/Application Support/tunegrab` and `~/Library/Logs/tunegrab`; on Linux
`~/.config/tunegrab`, `~/.local/share/tunegrab` and `~/.local/state/tunegrab`); delete
those too if you want them gone. Your music folder is never touched.

</details>

<details>
<summary><b>For developers</b></summary>

<br>

Python 3.11+, managed with **uv**. Typer + Rich (CLI), httpx, pydantic, FastAPI (local app),
ytmusicapi + rapidfuzz (matching), yt-dlp, mutagen, SQLite.

```powershell
uv sync                         # install everything, including the developer tools
uv run pytest -q                # unit tests (network mocked)
uv run pytest -m live -q        # real-network smoke tests
uv run ruff check --fix . ; uv run ruff format . ; uv run mypy src
uv run python scripts/check_design.py   # UI colour-contrast check
```

| Path | What's there |
|---|---|
| `src/tunegrab/metadata/` | Spotify Web API and public-page providers |
| `src/tunegrab/matching/` | YouTube Music / YouTube search and scoring |
| `src/tunegrab/download/`, `tagging/` | yt-dlp (with YouTube workarounds), ffmpeg and mutagen |
| `src/tunegrab/video.py` | Video, clip and audio-from-video downloads |
| `src/tunegrab/storage.py` | Disk usage report and `tunegrab clean` |
| `src/tunegrab/library/` | SQLite archive, history, overrides |
| `src/tunegrab/web/` | FastAPI app and the single-page UI |
| `docs/ARCHITECTURE.md` | Design |

Changes per version: [`CHANGELOG.md`](https://github.com/HAVEKAUTION/tunegrab/blob/main/CHANGELOG.md).

</details>

---

## Project status

TuneGrab is a personal project, shared as-is. I fix what breaks for my own use and push those
fixes here, but I don't take feature requests or offer support. Forks are welcome.

## License

TuneGrab is free software under the [GNU General Public License v3.0 or later](https://github.com/HAVEKAUTION/tunegrab/blob/main/LICENSE).
You can use, change and share it; if you share a changed version, it must stay under the same licence.

TuneGrab builds on [yt-dlp](https://github.com/yt-dlp/yt-dlp),
[ytmusicapi](https://github.com/sigma67/ytmusicapi), [mutagen](https://github.com/quodlibet/mutagen),
[LRCLIB](https://lrclib.net) and other open-source projects. It is not affiliated with Spotify or YouTube.
Use it only for music you're allowed to download where you live.
