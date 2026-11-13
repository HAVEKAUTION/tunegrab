# TuneGrab installer for Windows.
#
#   irm https://raw.githubusercontent.com/HAVEKAUTION/tunegrab/main/install.ps1 | iex
#
# What it does (read it first - that's the point of a short script):
#   1. Installs uv, ffmpeg and Deno with winget, if they're missing.
#   2. Downloads the latest TuneGrab release into %LOCALAPPDATA%\Programs\TuneGrab
#      An update replaces only TuneGrab's own files: the Python environment, your music
#      folder, .env and anything else you keep there are left alone.
#   3. Sets up its Python environment (uv sync), so the first start is quick.
#   4. Adds TuneGrab to the Start menu and the desktop, then starts it.
#
# Uninstall (your music, and settings in %LOCALAPPDATA%\tunegrab, are kept):
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/HAVEKAUTION/tunegrab/main/install.ps1))) -Uninstall

param(
    [string]$Dir = (Join-Path $env:LOCALAPPDATA "Programs\TuneGrab"),
    [switch]$Uninstall,
    [switch]$NoStart,
    [switch]$NoShortcuts,
    # Install from a zip on disk instead of downloading (used by scripts/test_install.ps1)
    [string]$ZipPath = ""
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

# The install folder can hold things TuneGrab didn't put there: the music folder
# (the default "downloads" lives here), a .env with Spotify credentials, anything else.
# Updates and uninstalls only ever touch the top-level names the installer itself put
# there, recorded in this manifest.
$Manifest = ".tunegrab-installed"
# What releases up to 0.4.1 contained (used when there's no manifest yet).
$KnownAppItems = @(".env.example", ".gitattributes", ".github", ".gitignore", ".python-version",
    "CHANGELOG.md", "LICENSE", "README.md", "TuneGrab.bat", "assets", "docs", "install.ps1",
    "pyproject.toml", "scripts", "src", "tests", "uv.lock")

function Installed-Items {
    $path = Join-Path $Dir $Manifest
    if (Test-Path $path) { return @(Get-Content $path | Where-Object { $_ -and $_ -notmatch '[\\/]' }) }
    return $KnownAppItems
}

function Stop-TuneGrab {
    Get-CimInstance Win32_Process -Filter "Name = 'tunegrab.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -like "$Dir*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

if ($Uninstall) {
    Stop-TuneGrab
    Remove-Item $StartMenu, $Desktop -Force -ErrorAction SilentlyContinue
    $left = @()
    if (Test-Path $Dir) {
        foreach ($name in @(Installed-Items) + @(".venv", $Manifest)) {
            $item = Join-Path $Dir $name
            if (Test-Path -LiteralPath $item) { Remove-Item -LiteralPath $item -Recurse -Force }
        }
        $left = @(Get-ChildItem $Dir -Force)
        if ($left.Count -eq 0) { Remove-Item $Dir -Force }
    }
    Say "TuneGrab removed from $Dir."
    if ($left.Count -gt 0) {
        Write-Host "    Kept what TuneGrab didn't install there (your music is in 'downloads' unless"
        Write-Host "    you chose another folder):"
        $left | ForEach-Object { Write-Host "      $($_.FullName)" }
    } else {
        Write-Host "    Your music folder was not touched."
    }
    Write-Host "    Settings and download history are in $env:LOCALAPPDATA\tunegrab -"
    Write-Host "    delete that folder too if you want them gone."
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
# Releases from 0.6.0 on carry a zip and SHA256SUMS; the installer checks the zip against it.
# Older releases (and no release at all) fall back to GitHub's source zip, as before.
$sumsUrl = $null
if ($ZipPath) {
    $tag = "from $ZipPath"
} else {
    try {
        $release = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest" -Headers @{ "User-Agent" = "tunegrab-installer" }
        $tag = $release.tag_name
        $zipUrl = "https://github.com/$Repo/archive/refs/tags/$tag.zip"
        $asset = $release.assets | Where-Object { $_.name -eq "tunegrab-$tag.zip" } | Select-Object -First 1
        $sums = $release.assets | Where-Object { $_.name -eq "SHA256SUMS" } | Select-Object -First 1
        if ($asset -and $sums) { $zipUrl = $asset.browser_download_url; $sumsUrl = $sums.browser_download_url }
    } catch {
        $tag = "main"
        $zipUrl = "https://github.com/$Repo/archive/refs/heads/main.zip"
    }
}
Say "Downloading TuneGrab $tag"
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("tunegrab-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory $tmp | Out-Null
try {
    $zip = Join-Path $tmp "tunegrab.zip"
    if ($ZipPath) { Copy-Item -LiteralPath $ZipPath -Destination $zip } else { Invoke-WebRequest $zipUrl -OutFile $zip -UseBasicParsing }
    if ($sumsUrl) {
        $line = (Invoke-RestMethod $sumsUrl -UseBasicParsing) -split "`n" | Where-Object { $_ -match "tunegrab-$([regex]::Escape($tag))\.zip" } | Select-Object -First 1
        $expected = if ($line) { ($line -split '\s+')[0].ToLower() } else { "" }
        $actual = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
        if (-not $expected -or $expected -ne $actual) {
            throw "The downloaded TuneGrab $tag doesn't match its published checksum. Nothing was changed; try again later."
        }
        Say "Checksum verified"
    }
    Expand-Archive $zip -DestinationPath $tmp
    $source = Get-ChildItem $tmp -Directory | Select-Object -First 1

    # A running TuneGrab locks its files: stop the one in this folder first.
    Stop-TuneGrab

    New-Item -ItemType Directory $Dir -Force | Out-Null
    # Replace only what the old release installed and the new one ships. Never mirror the
    # whole folder: that would delete the music folder and .env (it did up to 0.4.1).
    $previous = if (Test-Path $Dir) { @(Installed-Items) } else { @() }
    $items = @(Get-ChildItem $source.FullName -Force)
    $names = @($items | ForEach-Object { $_.Name })
    foreach ($old in $previous) {
        $path = Join-Path $Dir $old
        if ($names -notcontains $old -and (Test-Path -LiteralPath $path)) {
            Remove-Item -LiteralPath $path -Recurse -Force  # dropped from the new release
        }
    }
    foreach ($item in $items) {
        $target = Join-Path $Dir $item.Name
        if ($item.PSIsContainer) {
            # /MIR inside one of the release's own folders only (removes stale source files)
            robocopy $item.FullName $target /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
            if ($LASTEXITCODE -ge 8) { throw "Copying $($item.Name) to $Dir failed (robocopy $LASTEXITCODE)." }
        } else {
            Copy-Item -LiteralPath $item.FullName -Destination $target -Force
        }
    }
    Set-Content -Path (Join-Path $Dir $Manifest) -Value $names -Encoding utf8
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
