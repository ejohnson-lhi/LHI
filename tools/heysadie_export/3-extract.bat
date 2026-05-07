@echo off
REM ============================================================
REM Phase 3: Bulk extraction
REM   Visits every dashboard page, captures all JSON API responses,
REM   walks the analytics page calling into each call to capture
REM   detail data. Skips audio download (Phase 4 handles that
REM   separately so you can re-run audio without re-extracting).
REM
REM   Reuses discovery.json from the most recent reconnaissance run.
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
echo === Phase 3: Bulk Extraction ===
echo (Reusing discovery.json from latest export. No audio in this phase.)
echo.
python export.py --skip-recon --no-audio
echo.
echo Phase 3 complete. Pages and API responses saved.
echo Next: run 4-audio.bat to download call recordings.
echo.
pause
