# TuneGrab installer for Windows.
#
#   irm https://raw.githubusercontent.com/HAVEKAUTION/tunegrab/main/install.ps1 | iex
#
# What it does (read it first - that's the point of a short script):
#   1. Installs uv, ffmpeg and Deno with winget, if they're missing.
#   2. Downloads the latest TuneGrab release into %LOCALAPPDATA%\Programs\TuneGrab
#      (an update keeps the existing Python environment).
#   3. Sets up its Python environment (uv sync), so the first start is quick.
#   4. Adds TuneGrab to the Start menu and the desktop, then starts it.
#
# Uninstall (your music, and settings in %LOCALAPPDATA%\tunegrab, are kept):
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/HAVEKAUTION/tunegrab/main/install.ps1))) -Uninstall

param(
    [string]$Dir = (Join-Path $env:LOCALAPPDATA "Programs\TuneGrab"),
    [switch]$Uninstall,
    [switch]$NoStart,
    [switch]$NoShortcuts
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # Invoke-WebRequest is very slow with a progress bar
$Repo = "HAVEKAUTION/tunegrab"
$StartMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "TuneGrab.lnk"
$Desktop = Join-Path ([Environment]::GetFolderPath("Desktop")) "TuneGrab.lnk"

function Say($text) { Write-Host "==> $text" -ForegroundColor Green }

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
    # winget puts portable apps (ffmpeg) behind a Links folder; Deno's own installer uses ~\.deno\bin
    foreach ($extra in @("$env:LOCALAPPDATA\Microsoft\WinGet\Links", "$env:USERPROFILE\.deno\bin", "$env:USERPROFILE\.local\bin")) {
        if ((Test-Path $extra) -and ($env:Path -notlike "*$extra*")) { $env:Path += ";$extra" }
    }
}

function Have($command) { [bool](Get-Command $command -ErrorAction SilentlyContinue) }

if ($Uninstall) {
    Get-CimInstance Win32_Process -Filter "Name = 'tunegrab.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -like "$Dir*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Remove-Item $StartMenu, $Desktop -Force -ErrorAction SilentlyContinue
    if (Test-Path $Dir) { Remove-Item $Dir -Recurse -Force }
    Say "TuneGrab removed from $Dir."
    Write-Host "    Your music was not touched. Settings and download history are in"
    Write-Host "    $env:LOCALAPPDATA\tunegrab - delete that folder too if you want them gone."
    Write-Host "    uv, ffmpeg and Deno were left installed (winget uninstall to remove them)."
    return
}

# --- 1. tools ---------------------------------------------------------------------------
Refresh-Path
$needed = @()
if (-not (Have "uv")) { $needed += "astral-sh.uv" }
if (-not (Have "ffmpeg")) { $needed += "Gyan.FFmpeg" }
if (-not ((Have "deno") -or (Have "node"))) { $needed += "DenoLand.Deno" }
if ($needed.Count -gt 0) {
    if (-not (Have "winget")) {
        throw "winget isn't available. Install 'App Installer' from the Microsoft Store, or install these yourself: $($needed -join ', ')"
    }
    foreach ($id in $needed) {
        Say "Installing $id"
        winget install --id $id --exact --silent --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {  # that code = already installed
            throw "winget couldn't install $id (exit code $LASTEXITCODE)."
        }
    }
    Refresh-Path
}
foreach ($tool in @("uv", "ffmpeg")) {
    if (-not (Have $tool)) { throw "$tool was installed but isn't on PATH yet. Open a new PowerShell window and run the installer again." }
}

# --- 2. TuneGrab --------------------------------------------------------------------------
try {
    $release = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest" -Headers @{ "User-Agent" = "tunegrab-installer" }
    $tag = $release.tag_name
    $zipUrl = "https://github.com/$Repo/archive/refs/tags/$tag.zip"
} catch {
    $tag = "main"
    $zipUrl = "https://github.com/$Repo/archive/refs/heads/main.zip"
}
Say "Downloading TuneGrab $tag"
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("tunegrab-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory $tmp | Out-Null
try {
    $zip = Join-Path $tmp "tunegrab.zip"
    Invoke-WebRequest $zipUrl -OutFile $zip -UseBasicParsing
    Expand-Archive $zip -DestinationPath $tmp
    $source = Get-ChildItem $tmp -Directory | Select-Object -First 1

    # A running TuneGrab locks its files: stop the one in this folder first.
    Get-CimInstance Win32_Process -Filter "Name = 'tunegrab.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -like "$Dir*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

    New-Item -ItemType Directory $Dir -Force | Out-Null
    # Mirror the release into $Dir, but keep the Python environment (.venv) for speed.
    robocopy $source.FullName $Dir /MIR /XD .venv /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copying files to $Dir failed (robocopy $LASTEXITCODE)." }
} finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

# --- 3. Python environment -------------------------------------------------------------------
Say "Setting up (this takes a minute the first time)"
$env:UV_LINK_MODE = "copy"  # the uv cache and $Dir may be on different drives
Push-Location $Dir
try {
    uv sync --locked --no-dev --quiet
    if ($LASTEXITCODE -ne 0) {
        Remove-Item .venv -Recurse -Force -ErrorAction SilentlyContinue  # e.g. moved/broken env
        uv sync --locked --no-dev --quiet
        if ($LASTEXITCODE -ne 0) { throw "Setting up TuneGrab's Python environment failed (see above)." }
    }
} finally { Pop-Location }

# --- 4. shortcuts ---------------------------------------------------------------------------
$shell = New-Object -ComObject WScript.Shell
foreach ($path in $(if ($NoShortcuts) { @() } else { @($StartMenu, $Desktop) })) {
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = Join-Path $Dir "TuneGrab.bat"
    $link.WorkingDirectory = $Dir
    $link.Description = "TuneGrab: music and video downloader"
    $icon = Join-Path $Dir "assets\tunegrab.ico"
    if (Test-Path $icon) { $link.IconLocation = $icon }
    $link.Save()
}

Say "TuneGrab $tag is installed in $Dir"
Write-Host "    Start it from the Start menu or the desktop shortcut."
if (-not $NoStart) {
    Start-Process -FilePath (Join-Path $Dir "TuneGrab.bat") -WorkingDirectory $Dir
}
