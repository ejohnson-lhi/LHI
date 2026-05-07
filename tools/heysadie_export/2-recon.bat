@echo off
REM ============================================================
REM Phase 2: Reconnaissance
REM   Visits /admin/settings and /admin/analytics once each,
REM   discovers the working selectors for tabs, call rows, and
REM   pagination. Saves discovery.json for inspection and reuse
REM   by the extraction phase.
REM
REM Run this once, then look at the discovery.json in the newest
REM exports/ subfolder before running 3-extract.bat.
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
echo === Phase 2: Reconnaissance ===
python export.py --recon-only
echo.
echo Phase 2 complete. Inspect the latest exports\^<timestamp^>\discovery.json
echo to verify the discovered selectors look correct, then run 3-extract.bat.
echo.
pause
