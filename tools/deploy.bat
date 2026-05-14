@echo off
REM Stage + commit + push from Windows, then ssh to droplet, pull, and
REM restart the agent service. Double-click to run.
REM
REM Commit message comes from tools/.deploy_msg (gitignored). Claude updates
REM that file each time code changes; you just hit Enter to use it. If the
REM file is missing or empty, you'll be prompted to type a message.

setlocal enabledelayedexpansion

if exist "%SystemRoot%\Sysnative\OpenSSH\ssh.exe" (
    set "SSH=%SystemRoot%\Sysnative\OpenSSH\ssh.exe"
) else if exist "%SystemRoot%\System32\OpenSSH\ssh.exe" (
    set "SSH=%SystemRoot%\System32\OpenSSH\ssh.exe"
) else (
    set "SSH=ssh"
)

set "REMOTE=iris@64.23.167.164"
set "PROJECT_DIR=%~dp0.."
set "MSG_FILE=%~dp0.deploy_msg"

cd /d "%PROJECT_DIR%"

echo === Local git status ===
git status --short
echo.

REM Decide commit mode:
REM   - If .deploy_msg exists and is non-empty: preview it, let user
REM     accept/override/skip. Use `git commit -F file` so multi-line
REM     content with quotes / parens / ampersands works correctly (the
REM     old `-m "!MSG!"` form broke on embedded double quotes — git
REM     would parse the rest of the message as filenames).
REM   - If .deploy_msg missing/empty: prompt for a single-line message,
REM     write it to .deploy_msg, then use `-F`.
REM   - "skip" or blank override = push+deploy without commit.

set "COMMIT_FROM_FILE=false"

if exist "%MSG_FILE%" (
    for %%I in ("%MSG_FILE%") do set "MSG_SIZE=%%~zI"
    if !MSG_SIZE! GTR 0 (
        echo Commit message from .deploy_msg:
        echo ----------------------------------------
        type "%MSG_FILE%"
        echo.
        echo ----------------------------------------
        set "OVERRIDE="
        set /p "OVERRIDE=Press Enter to use this, type a new message to override (single line), or 'skip' to push without committing: "
        if /i "!OVERRIDE!"=="skip" (
            set "COMMIT_FROM_FILE=false"
        ) else if not "!OVERRIDE!"=="" (
            REM Override: write to the file so git -F can read it.
            > "%MSG_FILE%" echo !OVERRIDE!
            set "COMMIT_FROM_FILE=true"
        ) else (
            set "COMMIT_FROM_FILE=true"
        )
    )
)

if "%COMMIT_FROM_FILE%"=="false" (
    if not exist "%MSG_FILE%" (
        set "NEW_MSG="
        set /p "NEW_MSG=Commit message (single line, blank to skip commit): "
        if not "!NEW_MSG!"=="" (
            > "%MSG_FILE%" echo !NEW_MSG!
            set "COMMIT_FROM_FILE=true"
        )
    )
)
echo.

if "%COMMIT_FROM_FILE%"=="true" (
    echo === Staging and committing ===
    git add -A
    git commit -F "%MSG_FILE%"
    if errorlevel 1 (
        echo.
        echo Commit failed - nothing to commit, or other error.
        echo Continuing to push + deploy in case earlier work is unpushed.
    )
    echo.
)

echo === Pushing to GitHub ===
git push
if errorlevel 1 (
    echo.
    echo Push failed. Aborting deploy.
    goto :end
)
echo.

echo === Pulling on droplet and restarting agent service ===
REM On the droplet: pull, sync the systemd unit file (idempotent — only
REM reloads if cp actually changed anything since cp -u checks mtime),
REM then restart the agent service.
"%SSH%" %REMOTE% "cd /opt/iris-backend && git pull && sudo cp -u deploy/iris-agent.service /etc/systemd/system/iris-agent.service && sudo systemctl daemon-reload && sudo systemctl restart iris-agent.service && echo --- service status --- && sudo systemctl status iris-agent.service --no-pager -l | head -20"

REM Clear the message file so a stale message doesn't get reused on the next run.
if exist "%MSG_FILE%" del "%MSG_FILE%"

:end
echo.
echo === Press any key to close this window ===
pause >nul
endlocal
