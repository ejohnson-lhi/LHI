@echo off
REM Sync Cloudbeds admin login credentials (email/password/TOTP secret) from
REM local backend/.env to the droplet's /opt/iris-backend/backend/.env, then
REM restart iris-backend so the new env loads. Secrets pipe via SSH stdin,
REM never as command-line args. See sync_cloudbeds_creds.py for details.

setlocal

REM Prefer the backend venv's Python (matches the project's Python version),
REM fall back to whatever python is on PATH.
set "PY=%~dp0..\backend\.venv\Scripts\python.exe"
if not exist "%PY%" (
    set "PY=python"
)

"%PY%" "%~dp0sync_cloudbeds_creds.py"

echo.
echo === Press any key to close this window ===
pause >nul
endlocal
