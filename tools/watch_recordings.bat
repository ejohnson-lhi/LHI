@echo off
REM Continuously poll the droplet every 30 seconds and pull new recordings,
REM transcripts, and TTS-cache WAVs. Leave this window open while testing;
REM close it (or Ctrl-C) when done.
REM
REM Walks subdirectories so files under recordings/tts_cache/ end up
REM under recordings\tts_cache\ on Windows.

setlocal enabledelayedexpansion

set "REMOTE=iris@64.23.167.164"
set "REMOTE_DIR=/opt/iris-backend/recordings"
set "LOCAL_DIR=%~dp0..\recordings"
set "INTERVAL=30"

if exist "%SystemRoot%\Sysnative\OpenSSH\ssh.exe" (
    set "SSH=%SystemRoot%\Sysnative\OpenSSH\ssh.exe"
    set "SCP=%SystemRoot%\Sysnative\OpenSSH\scp.exe"
) else if exist "%SystemRoot%\System32\OpenSSH\ssh.exe" (
    set "SSH=%SystemRoot%\System32\OpenSSH\ssh.exe"
    set "SCP=%SystemRoot%\System32\OpenSSH\scp.exe"
) else (
    set "SSH=ssh"
    set "SCP=scp"
)

if not exist "%LOCAL_DIR%" mkdir "%LOCAL_DIR%"

echo === Watching droplet for new recordings (every %INTERVAL%s) ===
echo Local: %LOCAL_DIR%
echo Press Ctrl-C or close this window to stop.
echo.

:loop
echo [%TIME%] checking...
set "TMP=%TEMP%\iris_watch_%RANDOM%.txt"
"%SSH%" %REMOTE% "find %REMOTE_DIR% -type f \( -name '*.ogg' -o -name '*.wav' -o -name '*.json' \) -printf '%%P\n'" > "%TMP%" 2>nul
if errorlevel 1 (
    echo [%TIME%] ssh failed, will retry next cycle.
    del "%TMP%" 2>nul
    goto :sleep
)

set /a NEW=0
for /f "usebackq delims=" %%F in ("%TMP%") do (
    set "REL=%%F"
    set "REL_WIN=!REL:/=\!"
    set "LOCAL_FILE=%LOCAL_DIR%\!REL_WIN!"
    if not exist "!LOCAL_FILE!" (
        for %%D in ("!LOCAL_FILE!") do (
            if not exist "%%~dpD" mkdir "%%~dpD" >nul 2>&1
        )
        echo [%TIME%] pulling !REL!
        "%SCP%" -q "%REMOTE%:%REMOTE_DIR%/!REL!" "!LOCAL_FILE!"
        set /a NEW+=1
    )
)
del "%TMP%" 2>nul

if !NEW! gtr 0 echo [%TIME%] !NEW! new files added

:sleep
timeout /t %INTERVAL% /nobreak >nul
goto :loop
