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

REM Commit mode — non-interactive by default to avoid the Enter-key tax:
REM   - If .deploy_msg exists and is non-empty: preview it for context,
REM     commit with `git commit -F file` (no quote/paren/ampersand issues),
REM     and continue without prompting.
REM   - If .deploy_msg is missing or empty: skip commit silently, still
REM     push and deploy in case earlier work hasn't been pushed yet.
REM
REM To override the commit message: edit tools/.deploy_msg before running.
REM To skip a commit entirely: delete tools/.deploy_msg before running.
REM To pass an interactive override at runtime: run `deploy.bat -i`.

set "INTERACTIVE=false"
if /i "%~1"=="-i" set "INTERACTIVE=true"
if /i "%~1"=="--interactive" set "INTERACTIVE=true"

set "COMMIT_FROM_FILE=false"

if exist "%MSG_FILE%" (
    for %%I in ("%MSG_FILE%") do set "MSG_SIZE=%%~zI"
    if !MSG_SIZE! GTR 0 (
        echo Commit message from .deploy_msg:
        echo ----------------------------------------
        type "%MSG_FILE%"
        echo.
        echo ----------------------------------------
        if "!INTERACTIVE!"=="true" (
            set "OVERRIDE="
            set /p "OVERRIDE=Press Enter to use this, type a new message to override (single line), or 'skip' to push without committing: "
            if /i "!OVERRIDE!"=="skip" (
                set "COMMIT_FROM_FILE=false"
            ) else if not "!OVERRIDE!"=="" (
                > "%MSG_FILE%" echo !OVERRIDE!
                set "COMMIT_FROM_FILE=true"
            ) else (
                set "COMMIT_FROM_FILE=true"
            )
        ) else (
            REM Non-interactive: just commit with whatever's in the file.
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

echo === Pulling on droplet, syncing deps, restarting both services ===
REM On the droplet: pull, run pip install (idempotent; picks up any new
REM dependency from backend/pyproject.toml so we don't crash-loop the
REM service on a missing module — we've hit this with phonenumbers and
REM python-multipart), sync the systemd unit file (cp -u skips if no
REM change), then restart BOTH services. iris-agent is the LiveKit
REM worker; iris-backend is the uvicorn FastAPI that serves the guest
REM portal + the /dcs/* relay. They have separate codepaths but both
REM live in this repo, so a deploy that doesn't restart both leaves
REM one of them running stale code.
"%SSH%" %REMOTE% "cd /opt/iris-backend && git pull && sudo -u iris /opt/iris-backend/backend/.venv/bin/pip install -e /opt/iris-backend/backend && sudo cp -u deploy/iris-agent.service /etc/systemd/system/iris-agent.service && sudo systemctl daemon-reload && sudo systemctl restart iris-agent.service && sudo systemctl restart iris-backend.service && echo --- iris-agent --- && sudo systemctl status iris-agent.service --no-pager -l | head -12 && echo --- iris-backend --- && sudo systemctl status iris-backend.service --no-pager -l | head -12"

REM Clear the message file so a stale message doesn't get reused on the next run.
if exist "%MSG_FILE%" del "%MSG_FILE%"

:end
echo.
echo === Press any key to close this window ===
pause >nul
endlocal
