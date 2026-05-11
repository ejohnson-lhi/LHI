@echo off
REM Pull new Iris call recordings + transcripts (and pre-cached TTS WAVs)
REM from the droplet to the local project's recordings/ folder. Skips files
REM already present. Walks subdirectories so files under recordings/tts_cache/
REM end up under recordings\tts_cache\ on Windows.
REM
REM Uses Windows' built-in OpenSSH (System32\OpenSSH) by absolute path so
REM PATH lookup quirks don't matter, and so it shares your Windows ssh-agent.

setlocal enabledelayedexpansion

set "REMOTE=iris@64.23.167.164"
set "REMOTE_DIR=/opt/iris-backend/recordings"
set "LOCAL_DIR=%~dp0..\recordings"

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

echo Local destination: %LOCAL_DIR%
echo Using ssh: %SSH%
echo Listing remote files (recursive) on %REMOTE%...

set "TMP=%TEMP%\iris_remote_%RANDOM%.txt"
REM find -printf '%P\n' gives paths relative to REMOTE_DIR (no leading slash).
REM The %%P escapes one % in batch so the remote bash sees the literal %P.
"%SSH%" %REMOTE% "find %REMOTE_DIR% -type f \( -name '*.ogg' -o -name '*.wav' -o -name '*.json' \) -printf '%%P\n'" > "%TMP%"
if errorlevel 1 (
    echo.
    echo ERROR: ssh failed.
    del "%TMP%" 2>nul
    goto :end
)

set /a NEW=0
set /a TOTAL=0
for /f "usebackq delims=" %%F in ("%TMP%") do (
    set "REL=%%F"
    set /a TOTAL+=1
    REM Convert remote forward slashes to Windows backslashes.
    set "REL_WIN=!REL:/=\!"
    set "LOCAL_FILE=%LOCAL_DIR%\!REL_WIN!"
    if not exist "!LOCAL_FILE!" (
        REM Extract parent dir from the full local path and mkdir it.
        for %%D in ("!LOCAL_FILE!") do (
            if not exist "%%~dpD" mkdir "%%~dpD" >nul 2>&1
        )
        echo Pulling: !REL!
        "%SCP%" -q "%REMOTE%:%REMOTE_DIR%/!REL!" "!LOCAL_FILE!"
        set /a NEW+=1
    )
)

del "%TMP%" 2>nul
echo.
if !NEW! equ 0 (
    echo Up to date - !TOTAL! files on droplet, all already local.
) else (
    echo Done. !NEW! new files added; !TOTAL! total on droplet.
)

:end
endlocal
echo.
echo === Press any key to close this window ===
pause >nul
