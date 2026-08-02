@echo off
REM ====================================================================
REM  Launches the YT-M Downloader GUI.
REM  Double-click this file.
REM ====================================================================
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py "%~dp0yt_music_downloader.py"
) else (
    python "%~dp0yt_music_downloader.py"
)

if %errorlevel% neq 0 (
    echo.
    echo Something went wrong. If Python isn't installed, get it from
    echo https://www.python.org/downloads/ ^(check "Add to PATH"^),
    echo then run setup.bat once.
    pause
)
