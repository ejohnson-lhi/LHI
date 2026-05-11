@echo off
REM "git push" for TTS cache deletions.
REM
REM Workflow:
REM   1. Listen to WAVs in recordings\tts_cache\ in VLC / Media Player.
REM   2. Delete the bad ones locally (Explorer, "del", whatever).
REM   3. Run this script. It compares your local WAV list to the droplet's
REM      and removes from the droplet anything not present locally.
REM   4. On the next call, the agent's prewarm sees the missing WAVs,
REM      drops the corresponding cache entries, and the next time those
REM      phrases come up the LLM, they get resynthesized fresh.
REM
REM Caveat: stop watch_recordings.bat before doing this, or it will
REM re-pull the WAVs you just deleted in the next polling cycle.

setlocal enabledelayedexpansion

set "REMOTE=iris@64.23.167.164"
set "REMOTE_DIR=/opt/iris-backend/recordings/tts_cache"
set "LOCAL_DIR=%~dp0..\recordings\tts_cache"

if exist "%SystemRoot%\Sysnative\OpenSSH\ssh.exe" (
    set "SSH=%SystemRoot%\Sysnative\OpenSSH\ssh.exe"
) else if exist "%SystemRoot%\System32\OpenSSH\ssh.exe" (
    set "SSH=%SystemRoot%\System32\OpenSSH\ssh.exe"
) else (
    set "SSH=ssh"
)

if not exist "%LOCAL_DIR%" (
    echo No local %LOCAL_DIR% folder yet. Run sync_recordings.bat first
    echo so there's something to compare against.
    goto :end
)

echo Comparing local WAVs to droplet...
set "TMP_LOCAL=%TEMP%\iris_local_wavs_%RANDOM%.txt"
set "TMP_REMOTE=%TEMP%\iris_remote_wavs_%RANDOM%.txt"

dir /b "%LOCAL_DIR%\*.wav" > "%TMP_LOCAL%" 2>nul
"%SSH%" %REMOTE% "ls %REMOTE_DIR%/*.wav 2>/dev/null | xargs -n1 -r basename" > "%TMP_REMOTE%"

set /a TO_DELETE=0
for /f "usebackq delims=" %%R in ("%TMP_REMOTE%") do (
    findstr /x /c:"%%R" "%TMP_LOCAL%" >nul
    if errorlevel 1 (
        echo Will delete from droplet: %%R
        set /a TO_DELETE+=1
    )
)

if !TO_DELETE! equ 0 (
    echo No deletions to push - droplet WAVs already match local.
    goto :cleanup
)

echo.
set "CONFIRM="
set /p "CONFIRM=Push !TO_DELETE! deletion to droplet? [y/N]: "
if /i not "!CONFIRM!"=="y" (
    echo Aborted.
    goto :cleanup
)

echo.
echo Deleting...
for /f "usebackq delims=" %%R in ("%TMP_REMOTE%") do (
    findstr /x /c:"%%R" "%TMP_LOCAL%" >nul
    if errorlevel 1 (
        "%SSH%" %REMOTE% "rm -f %REMOTE_DIR%/%%R"
        echo   removed %%R
    )
)

echo.
echo Done. The next agent prewarm will validate the cache against the
echo trimmed WAV manifest and drop the corresponding entries.

:cleanup
del "%TMP_LOCAL%" 2>nul
del "%TMP_REMOTE%" 2>nul

:end
echo.
echo === Press any key to close this window ===
pause >nul
endlocal
