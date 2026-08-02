@echo off
REM ====================================================================
REM  One-time setup: installs yt-dlp and ffmpeg.
REM  Run this once before first use (double-click).
REM ====================================================================
echo Installing / updating yt-dlp, ytmusicapi, browser-cookie3 ...
where py >nul 2>nul
if %errorlevel%==0 (
    py -m pip install --upgrade yt-dlp ytmusicapi browser-cookie3
) else (
    python -m pip install --upgrade yt-dlp ytmusicapi browser-cookie3
)

echo.
echo Installing Deno (JavaScript runtime YouTube now requires) ...
where deno >nul 2>nul
if %errorlevel%==0 (
    echo Deno already installed. Good.
) else (
    where winget >nul 2>nul
    if %errorlevel%==0 (
        winget install --id=DenoLand.Deno -e --accept-source-agreements --accept-package-agreements
        echo.
        echo If Deno isn't found later, close and reopen this window so PATH refreshes.
    ) else (
        echo winget not available. Install Deno manually from https://deno.com
        echo ^(or run in PowerShell:  irm https://deno.land/install.ps1 ^| iex^)
    )
)

echo.
echo Installing ffmpeg (needed to create MP3s) ...
where ffmpeg >nul 2>nul
if %errorlevel%==0 (
    echo ffmpeg already on PATH. Good.
) else (
    where winget >nul 2>nul
    if %errorlevel%==0 (
        winget install --id=Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
        echo.
        echo If ffmpeg still isn't found later, close and reopen this window
        echo so PATH refreshes, or restart your PC.
    ) else (
        echo winget not available. Install ffmpeg manually:
        echo   https://www.gyan.dev/ffmpeg/builds/  ^(download the "release essentials" zip,
        echo   unzip it, and add its \bin folder to your PATH^).
    )
)

echo.
echo Clearing yt-dlp cache so it re-fetches the challenge-solver scripts ...
where py >nul 2>nul
if %errorlevel%==0 (
    py -m yt_dlp --rm-cache-dir
) else (
    python -m yt_dlp --rm-cache-dir
)

echo.
echo Setup finished. You can now run "Run YT-M Downloader.bat".
echo IMPORTANT: if Deno was just installed, fully close this window and the app,
echo then reopen the app so it can see Deno. The log should say "JS runtime found: deno".
pause
