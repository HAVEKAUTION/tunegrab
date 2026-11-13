# Installer scenario test: install, update twice, uninstall, all into a temporary folder.
# Checks that updates and the uninstall keep what the user put in the install folder
# (the default music folder "downloads" and a .env), and remove only TuneGrab's own files.
#
# Needs uv and ffmpeg on PATH (the installer would otherwise try winget).
# Run from the repo root:  powershell -ExecutionPolicy Bypass -File scripts/test_install.ps1
$ErrorActionPreference = "Stop"
$root = Join-Path ([IO.Path]::GetTempPath()) ("tunegrab-install-test-" + [guid]::NewGuid().ToString("N"))
$dir = Join-Path $root "TuneGrab"
$zip = Join-Path $root "tunegrab-test.zip"
New-Item -ItemType Directory $root | Out-Null

function Check($ok, $what) {
    if (-not $ok) { throw "FAILED: $what" }
    Write-Host "ok  $what"
}

try {
    # the committed tree, shaped like a GitHub zip (one top-level folder)
    git archive --format=zip --prefix=tunegrab-test/ -o $zip HEAD
    if ($LASTEXITCODE -ne 0) { throw "git archive failed" }

    & .\install.ps1 -Dir $dir -ZipPath $zip -NoStart -NoShortcuts
    Check (Test-Path (Join-Path $dir "src\tunegrab\__init__.py")) "first install copied the app"
    Check (Test-Path (Join-Path $dir ".venv")) "first install set up the environment"
    Check (Test-Path (Join-Path $dir ".tunegrab-installed")) "first install wrote its manifest"

    # what a user adds: music in the default folder, Spotify credentials, a stray file
    $song = Join-Path $dir "downloads\Artist\Album\01 Song.m4a"
    New-Item -ItemType Directory (Split-Path $song) -Force | Out-Null
    Set-Content $song "audio" -Encoding ascii
    Set-Content (Join-Path $dir ".env") "TUNEGRAB_SPOTIFY_CLIENT_ID=x" -Encoding ascii
    # a file the next release no longer ships must go
    Set-Content (Join-Path $dir "src\tunegrab\stale_module.py") "# old" -Encoding ascii

    foreach ($round in 1, 2) {
        & .\install.ps1 -Dir $dir -ZipPath $zip -NoStart -NoShortcuts
        Check ((Get-Content $song) -eq "audio") "update $round kept the music"
        Check (Test-Path (Join-Path $dir ".env")) "update $round kept .env"
    }
    Check (-not (Test-Path (Join-Path $dir "src\tunegrab\stale_module.py"))) "updates removed a stale app file"

    & .\install.ps1 -Dir $dir -Uninstall
    Check ((Get-Content $song) -eq "audio") "uninstall kept the music"
    Check (Test-Path (Join-Path $dir ".env")) "uninstall kept .env"
    Check (-not (Test-Path (Join-Path $dir "src"))) "uninstall removed the app"
    Check (-not (Test-Path (Join-Path $dir ".venv"))) "uninstall removed the environment"
    Write-Host "Installer scenario passed."
} finally {
    Remove-Item $root -Recurse -Force -ErrorAction SilentlyContinue
}
