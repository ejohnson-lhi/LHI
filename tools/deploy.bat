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

REM If tools\.deploy_env exists locally (gitignored Windows-side file
REM with KEY=VALUE lines), SCP it to the droplet so the SSH chain below
REM can merge it into /opt/iris-backend/backend/.env via
REM tools/sync_deploy_env.sh. Mirrors the .deploy_msg / commit-message
REM pattern so the user's source of truth for env-var tweaks stays on
REM this PC. If the file is absent, the sync step in the SSH chain
REM short-circuits with no error.
set "DEPLOY_ENV_FILE=%~dp0.deploy_env"
if exist "%DEPLOY_ENV_FILE%" (
    echo === Sending local .deploy_env to droplet ===
    "%SCP%" -q "%DEPLOY_ENV_FILE%" "%REMOTE%:/tmp/iris_deploy_env_in.txt"
    if errorlevel 1 (
        echo.
        echo .deploy_env SCP failed. Aborting deploy.
        goto :end
    )
) else (
    echo No tools\.deploy_env present; skipping env sync.
    echo ^(Copy tools\.deploy_env.template to tools\.deploy_env to enable.^)
)
echo.

echo === Pulling on droplet, syncing deps, restarting all services ===
REM On the droplet: pull, run pip install (idempotent; picks up any new
REM dependency from backend/pyproject.toml so we don't crash-loop the
REM service on a missing module — we've hit this with phonenumbers and
REM python-multipart), sync the systemd unit files (cp -u skips if no
REM change), then restart ALL THREE services. iris-agent is the LiveKit
REM worker; iris-backend is the uvicorn FastAPI that serves the guest
REM portal + the /dcs/* relay; iris-diarize-watcher polls for new OGGs
REM and runs the diarize batch at low priority, pausing/killing it
REM when a live call comes in (see tools/diarize/diarize_watcher.py).
REM
REM Legacy cron disable: the watcher replaces /etc/cron.d/iris-diarize.
REM First deploy renames the cron file to .disabled; subsequent deploys
REM no-op via the test -f check.
REM
REM systemctl enable for the watcher / smoke-test is idempotent — first
REM deploy enables for boot, subsequent deploys do nothing.
REM
REM Post-deploy smoke test: after restarting all three services, sleep
REM 30s (lets iris-agent finish prewarm + register with LiveKit), then
REM invoke iris_smoke_test.py once via SSH. Its exit code is propagated
REM by &&: a failed smoke test breaks the chain, the next echo lines
REM don't run, and deploy.bat shows the failure clearly. This catches
REM bad deploys that LOOK healthy (process up, registered) but crash
REM the per-call entrypoint — the failure mode that hid for 4 days
REM (2026-06-16 to 06-20) with the keywords/keyterm bug.
"%SSH%" %REMOTE% "cd /opt/iris-backend && git pull && (test -f /tmp/iris_deploy_env_in.txt && bash tools/sync_deploy_env.sh /tmp/iris_deploy_env_in.txt /opt/iris-backend/backend/.env && rm /tmp/iris_deploy_env_in.txt || true) && sudo -u iris /opt/iris-backend/backend/.venv/bin/pip install -e /opt/iris-backend/backend && sudo cp -u deploy/iris-agent.service /etc/systemd/system/iris-agent.service && sudo cp -u deploy/iris-backend.service /etc/systemd/system/iris-backend.service && sudo cp -u deploy/iris-diarize-watcher.service /etc/systemd/system/iris-diarize-watcher.service && sudo cp -u deploy/iris-smoke-test.service /etc/systemd/system/iris-smoke-test.service && sudo cp -u deploy/iris-smoke-test.timer /etc/systemd/system/iris-smoke-test.timer && (sudo test -f /etc/cron.d/iris-diarize && sudo mv /etc/cron.d/iris-diarize /etc/cron.d/iris-diarize.disabled || true) && sudo systemctl daemon-reload && sudo systemctl enable iris-diarize-watcher.service && sudo systemctl enable iris-smoke-test.timer && sudo systemctl restart iris-agent.service && sudo systemctl restart iris-backend.service && sudo systemctl restart iris-diarize-watcher.service && sudo systemctl restart iris-smoke-test.timer && echo --- iris-agent --- && sudo systemctl status iris-agent.service --no-pager -l | head -12 && echo --- iris-backend --- && sudo systemctl status iris-backend.service --no-pager -l | head -12 && echo --- iris-diarize-watcher --- && sudo systemctl status iris-diarize-watcher.service --no-pager -l | head -12 && echo --- post-deploy smoke test (waiting 30s for agent prewarm + register) --- && sleep 30 && (sudo systemctl start --wait iris-smoke-test.service ; sudo journalctl -u iris-smoke-test.service --no-pager -n 40 ; sudo systemctl is-failed --quiet iris-smoke-test.service && exit 1 || true)"
if errorlevel 1 (
    echo.
    echo *** DEPLOY ALERT: SSH chain returned non-zero. Likely the smoke test FAILED.
    echo *** Review the iris-smoke-test journal output above. If the failure says
    echo *** "Missing required env vars", set IRIS_SMOKE_TEST_TO / IRIS_SMOKE_TEST_FROM
    echo *** / IRIS_SMOKE_TEST_ALERT_TO / TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN in
    echo *** /opt/iris-backend/backend/.env and rerun this script.
    echo.
)

REM Clear the message file so a stale message doesn't get reused on the next run.
if exist "%MSG_FILE%" del "%MSG_FILE%"

:end
echo.
echo === Press any key to close this window ===
pause >nul
endlocal
