@echo off
REM ============================================================
REM Phase 1: Authentication
REM   Logs into Hey Sadie via Clerk, saves session.json for reuse
REM   by later phases. Opens a visible browser so you can complete
REM   any 2FA / captcha challenge manually.
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
echo === Phase 1: Authentication ===
echo (Visible browser will open. Complete login if prompted.)
echo.
python export.py --auth-only --headed
echo.
echo Phase 1 complete. Session saved to session.json.
echo Next: run 2-recon.bat to discover Hey Sadie's UI structure.
echo.
pause
