@echo off
rem TuneGrab launcher: double-click to open TuneGrab in your browser.
rem Closing this window stops TuneGrab.
setlocal
rem the uv cache and this folder may be on different drives: copy instead of hardlinking
set UV_LINK_MODE=copy
cd /d "%~dp0"
title TuneGrab

where uv >nul 2>nul
if errorlevel 1 (
    echo TuneGrab needs "uv" to run. Install it with:
    echo     winget install astral-sh.uv
    echo then double-click TuneGrab again.
    pause
    exit /b 1
)

rem Check the environment works. Moving the folder breaks it, so rebuild it if needed.
uv run --locked tunegrab --version >nul 2>nul
if errorlevel 1 (
    echo Setting up TuneGrab - this takes a minute the first time...
    if exist .venv rmdir /s /q .venv
    uv sync --locked
    if errorlevel 1 (
        echo.
        echo Setup failed. Check your internet connection and try again.
        pause
        exit /b 1
    )
)

echo TuneGrab is starting. Your browser opens when it's ready.
echo Keep this window open while downloading - closing it stops TuneGrab.
echo.
uv run --locked tunegrab serve --port 8765
if errorlevel 1 (
    echo.
    echo TuneGrab stopped because of the error above.
    echo For a full check, run:  uv run tunegrab doctor
    pause
)
endlocal
