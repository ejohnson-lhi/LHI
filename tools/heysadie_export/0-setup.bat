@echo off
REM ============================================================
REM Hey Sadie Export - One-time setup
REM   Creates a Python virtual environment, installs dependencies,
REM   installs Playwright's Chromium browser, and creates .env from
REM   .env.example if not present.
REM ============================================================

setlocal
cd /d "%~dp0"

echo.
echo === Hey Sadie Export - Setup ===
echo.

REM Verify Python is installed
where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python is not on PATH.
    echo Install Python 3.9 or newer from https://www.python.org/downloads/
    pause
    exit /b 1
)

REM Create virtual environment
if not exist .venv (
    echo Creating virtual environment in .venv\
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )
) else (
    echo Virtual environment already exists, skipping creation.
)

REM Activate venv and install Python dependencies
echo.
echo Installing Python dependencies...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip --quiet
pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: pip install failed.
    pause
    exit /b 1
)

REM Install Playwright Chromium
echo.
echo Installing Playwright Chromium browser (one-time, ~150MB)...
playwright install chromium
if errorlevel 1 (
    echo ERROR: Playwright install failed.
    pause
    exit /b 1
)

REM Create .env from template if missing
if not exist .env (
    echo.
    echo Creating .env from .env.example
    copy .env.example .env >nul
    echo.
    echo *******************************************************
    echo *  IMPORTANT: Edit .env now with your real Hey Sadie  *
    echo *  email and password before running 1-auth.bat       *
    echo *******************************************************
)

echo.
echo === Setup complete ===
echo Next: edit .env if needed, then run 1-auth.bat
echo.
pause
