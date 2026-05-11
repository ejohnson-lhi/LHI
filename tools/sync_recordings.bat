@echo off
REM Pull new Iris call recordings + transcripts from the droplet to the
REM local project's recordings/ folder. Skips files already present.
REM
REM Uses Windows' built-in OpenSSH (System32\OpenSSH) by absolute path so
REM PATH lookup quirks don't matter, and so it shares your Windows
REM ssh-agent (no passphrase prompt per run).

setlocal enabledelayedexpansion

set "REMOTE=iris@64.23.167.164"
set "REMOTE_DIR=/opt/iris-backend/recordings"
set "LOCAL_DIR=%~dp0..\recordings"

REM Find Windows OpenSSH. Sysnative is the 32-bit-process alias for the
REM real 64-bit System32; check it first in case this cmd is 32-bit.
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
echo Listing remote files on %REMOTE%...

set "TMP=%TEMP%\iris_remote_%RANDOM%.txt"
REM Filter on the remote side — only .ogg, .wav, and .json files.
"%SSH%" %REMOTE% "ls -1 %REMOTE_DIR% | grep -E '\.(ogg|wav|json)$'" > "%TMP%"
if errorlevel 1 (
    echo.
    echo ERROR: ssh failed.
    del "%TMP%" 2>nul
    goto :end
)

set /a NEW=0
set /a TOTAL=0
for /f "usebackq delims=" %%f in ("%TMP%") do (
    set "F=%%f"
    set /a TOTAL+=1
    if not exist "%LOCAL_DIR%\!F!" (
        echo Pulling: !F!
        "%SCP%" -q "%REMOTE%:%REMOTE_DIR%/!F!" "%LOCAL_DIR%"
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
