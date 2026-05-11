@echo off
REM Continuously poll the droplet every 30 seconds and pull any new
REM recordings/transcripts. Leave this window open while testing; close
REM it (or Ctrl-C) when done.
REM
REM Self-contained (does not call sync_recordings.bat) to avoid arg /
REM setlocal interactions between the two scripts.

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
"%SSH%" %REMOTE% "ls -1 %REMOTE_DIR% | grep -E '\.(ogg|json)$'" > "%TMP%" 2>nul
if errorlevel 1 (
    echo [%TIME%] ssh failed, will retry next cycle.
    del "%TMP%" 2>nul
    goto :sleep
)

set /a NEW=0
for /f "usebackq delims=" %%f in ("%TMP%") do (
    set "F=%%f"
    if not exist "%LOCAL_DIR%\!F!" (
        echo [%TIME%] pulling !F!
        "%SCP%" -q "%REMOTE%:%REMOTE_DIR%/!F!" "%LOCAL_DIR%"
        set /a NEW+=1
    )
)
del "%TMP%" 2>nul

if !NEW! gtr 0 echo [%TIME%] !NEW! new files added

:sleep
timeout /t %INTERVAL% /nobreak >nul
goto :loop
