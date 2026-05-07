@echo off
REM ============================================================
REM Lighthouse Inn AI Reservation Agent — Backend Setup
REM   One-time setup: creates venv, installs deps, copies .env template.
REM   Run this once, then double-click scripts\run_dev.bat to start.
REM ============================================================

setlocal
cd /d "%~dp0"

echo.
echo === Lighthouse Backend - Setup ===
echo.

REM --- Verify Python is on PATH ---
where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python is not on PATH.
    echo Install Python 3.10+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

REM --- Create virtual environment ---
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

REM --- Activate venv and install dependencies ---
call .venv\Scripts\activate.bat
echo.
echo Installing dependencies (this may take a minute)...
python -m pip install --upgrade pip --quiet
pip install -e .
if errorlevel 1 (
    echo ERROR: pip install failed.
    pause
    exit /b 1
)

REM --- Create .env from template if missing ---
REM Note: avoid parentheses in echo statements INSIDE if-blocks; the Windows
REM batch parser misinterprets them as block-closing parens and exits the
REM block early. Either escape with ^( and ^) or just avoid the chars.
if not exist .env goto :create_env
goto :env_done
:create_env
echo.
echo Creating .env from .env.example
copy .env.example .env >nul
echo.
echo *********************************************************
echo *  Edit .env with your real Twilio, Cloudbeds, Anthropic   *
echo *  Stripe, and Vapi credentials before running for real.   *
echo *  Stub responses work without real credentials.           *
echo *********************************************************
:env_done

REM --- Check for cloudflared ---
echo.
where cloudflared >nul 2>nul
if errorlevel 1 goto :no_cloudflared
echo cloudflared detected: ready for local dev.
goto :cf_done
:no_cloudflared
echo NOTE: cloudflared is not installed.
echo The local dev runner uses Cloudflare Tunnel to expose your local
echo backend to Vapi/Twilio over HTTPS. Install from:
echo   https://github.com/cloudflare/cloudflared/releases
echo Get the Windows installer named cloudflared-windows-amd64.msi
echo.
:cf_done

REM --- Create data folder for SQLite ---
if not exist data mkdir data

echo.
echo === Setup complete ===
echo.
echo Next steps:
echo   1. Edit .env if you have real API credentials to add (optional for stubs)
echo   2. Install cloudflared if you haven't (see note above)
echo   3. Double-click scripts\run_dev.bat to start the backend
echo.
echo The backend will run on http://localhost:8000 ^(port 8000, NOT 8080 -
echo 8080 is used by the GX-26 hotel system^).
echo.
pause
