@echo off
REM ============================================================
REM Full Hey Sadie export pipeline (auth + recon + extract + audio).
REM Use this for the actual data-pull runs after you've validated
REM each phase individually with the numbered .bat files.
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
echo === Hey Sadie Export - Full Pipeline ===
echo.
python export.py --headed
echo.
echo Full export complete.
echo.
pause
