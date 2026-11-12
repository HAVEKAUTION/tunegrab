# Setup

Do this once. Commands are given for Windows (PowerShell) and macOS/Linux.

## 1. Python 3.11+ and uv
uv installs and manages Python for you.

- Windows: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
- macOS/Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`

Check: `uv --version`, then `uv python install 3.12`.

## 2. ffmpeg (audio conversion)
- Windows: `winget install Gyan.FFmpeg`
- macOS: `brew install ffmpeg`
- Linux: `sudo apt install ffmpeg`

Check: `ffmpeg -version`

## 3. Deno (yt-dlp needs a JavaScript runtime for YouTube)
- Windows: `irm https://deno.land/install.ps1 | iex`
- macOS/Linux: `curl -fsSL https://deno.land/install.sh | sh`

Check: `deno --version`. Restart your terminal so it's on PATH.

## 4. Spotify metadata access — pick one

### Option A: Official Spotify Web API (most reliable, needs Premium)
Since February 2026, Development Mode apps require the developer account to have Spotify Premium.
1. Go to https://developer.spotify.com/dashboard and log in.
2. **Create app**. Name: anything. Redirect URI: `http://127.0.0.1:8888/callback`.
   Tick **Web API**.
3. Open the app → **Settings** → copy **Client ID** and **Client Secret**.
4. In **User Management**, add your own Spotify account email (needed for private playlists).
5. Put them in your project's `.env`:
   ```
   SPOTIFY_CLIENT_ID=xxxxxxxx
   SPOTIFY_CLIENT_SECRET=xxxxxxxx
   SPOTIFY_REDIRECT_URI=http://127.0.0.1:8888/callback
   ```

### Option B: No-API fallback (no Premium needed, more fragile)
The fallback provider reads the public embed pages (`open.spotify.com/embed/...`)
that Spotify serves to anyone for public tracks, albums and playlists. No key needed, but it
gets less metadata (no ISRC, fewer fields) and can break when Spotify changes the page.
Private playlists won't work this way.

Set up A if you can; TuneGrab tries it first and falls back to B automatically.

## 5. Get TuneGrab and start it
```
git clone https://github.com/HAVEKAUTION/tunegrab.git
cd tunegrab
uv sync
uv run tunegrab doctor     # checks everything above
uv run tunegrab serve      # opens the app in your browser
```
On Windows you can double-click `TuneGrab.bat` instead of the last three commands.
