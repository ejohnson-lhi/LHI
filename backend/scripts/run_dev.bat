@echo off
REM ============================================================
REM Local development runner
REM   Starts the FastAPI server (port 8000) and Cloudflare Tunnel
REM   in separate windows. Close both windows to stop.
REM
REM Prerequisites:
REM   1. Run setup steps in README.md (create venv, install deps)
REM   2. Install cloudflared: https://github.com/cloudflare/cloudflared
REM   3. Copy .env.example to .env and fill in real values
REM ============================================================

setlocal
cd /d "%~dp0\.."

REM --- Ensure cloudflared resolves even if this terminal inherited a
REM     pre-install PATH (Windows doesn't refresh PATH for already-open
REM     shells when an installer adds a new entry).
set "PATH=C:\Program Files (x86)\cloudflared\;%PATH%"

if not exist .venv\Scripts\activate.bat (
    echo ERROR: Virtual environment not found at .venv\
    echo Run the setup steps in README.md first.
    pause
    exit /b 1
)

if not exist .env (
    echo WARNING: .env file not found.
    echo Copy .env.example to .env and fill in your real values.
    echo Continuing anyway, but most endpoints will fail.
    echo.
    pause
)

call .venv\Scripts\activate.bat

echo.
echo === Starting Lighthouse Backend ===
echo FastAPI will start on http://localhost:8000
echo API docs: http://localhost:8000/docs
echo Health check: http://localhost:8000/health
echo.

start "Lighthouse Backend (FastAPI)" cmd /k uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

REM Wait for FastAPI to start before launching the tunnel
timeout /t 3 /nobreak > nul

echo.
echo === Starting Cloudflare Tunnel ===
echo Public HTTPS URL will appear in the tunnel window.
echo Copy that URL into Vapi/Twilio webhook config.
echo.

REM --protocol http2 forces TCP transport. Default is QUIC (UDP 7844),
REM which fails on networks that block/throttle UDP (common on Windows
REM with aggressive firewalls or behind some ISP CGNAT setups). HTTP/2
REM is slightly higher latency but reliably works.
start "Cloudflare Tunnel" cmd /k cloudflared tunnel --protocol http2 --url http://localhost:8000

echo.
echo Both processes are starting in separate windows.
echo Close those windows to stop. Press any key to close THIS window.
pause > nul
