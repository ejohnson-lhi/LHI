@echo off
REM Kick the diarize batch at idle CPU priority, behind a single-instance
REM lock. Called from watch_recordings.bat after new OGGs are pulled.
REM
REM Safe to call repeatedly: the batch itself is idempotent (scans for
REM OGGs without a sibling recordings/transcribed/<name>.json), and this
REM wrapper short-circuits when a previous invocation is still running.
REM
REM Priority: /low (IDLE_PRIORITY_CLASS) means the Whisper/pyannote work
REM only consumes cycles when nothing else wants them, so foreground work
REM on the PC isn't impacted. Note: Iris itself runs on the iris-backend
REM droplet, NOT this PC, so this priority class is purely to be a good
REM neighbor to whatever else is open here.
REM
REM Venv: defaults to tools\diarize\.venv\ (mirrors the droplet layout
REM documented in tools/diarize/README.md). Override with DIARIZE_VENV
REM if your venv lives elsewhere. If the venv is missing, the script
REM logs a clear message and exits 1 so the watcher keeps running.

setlocal enabledelayedexpansion

set "HERE=%~dp0"
set "DIARIZE_DIR=%HERE%diarize"
set "LOCK=%DIARIZE_DIR%\.run_lock"

if not defined DIARIZE_VENV set "DIARIZE_VENV=%DIARIZE_DIR%\.venv"
set "PYTHON=%DIARIZE_VENV%\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [%TIME%] [diarize] venv missing at %DIARIZE_VENV%
    echo [%TIME%] [diarize] set DIARIZE_VENV to your local venv path, or
    echo [%TIME%] [diarize] create one and pip install -r %DIARIZE_DIR%\requirements.txt
    exit /b 1
)

if exist "%LOCK%" (
    echo [%TIME%] [diarize] already running ^(lock present^); skipping
    exit /b 0
)

echo %DATE% %TIME% > "%LOCK%"
echo [%TIME%] [diarize] starting batch at idle priority
"%PYTHON%" "%DIARIZE_DIR%\diarize_batch.py"
set "RC=%ERRORLEVEL%"
del "%LOCK%" 2>nul
echo [%TIME%] [diarize] done ^(rc=%RC%^)
exit /b %RC%

endlocal
