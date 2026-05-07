@echo off
REM ============================================================
REM Phase 4: Audio downloads
REM   Scans the most recent export's api_captures/ for audio URLs
REM   and downloads them using the saved session credentials.
REM
REM   Idempotent — already-downloaded files are skipped, so you
REM   can re-run safely after a partial download.
REM ============================================================

setlocal
cd /d "%~dp0"

if not exist .venv\Scripts\activate.bat (
    echo ERROR: Virtual environment not found.
    echo Run setup.bat first.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat
echo.
echo === Phase 4: Audio Downloads ===
python export.py --audio-only
echo.
echo Phase 4 complete. Audio files in latest exports\^<timestamp^>\calls\audio\
echo.
pause
