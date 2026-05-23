"""Standalone test of the Cloudbeds login + 2FA flow.

Usage (from backend/ directory):
    .venv/Scripts/python.exe scripts/test_cloudbeds_login.py

Expects .env populated with:
    CLOUDBEDS_LOGIN_URL
    CLOUDBEDS_ADMIN_EMAIL
    CLOUDBEDS_ADMIN_PASSWORD
    CLOUDBEDS_TOTP_SECRET

Recommendation: set CLOUDBEDS_BROWSER_HEADLESS=false so you can WATCH the
browser navigate. If a step hangs or a selector misses, look at where the
real page differs from what the script expects, and tell me -- I'll
update the selector constants in app/tools/cloudbeds_browser.py.

This script does NOT generate a Pay-by-Link. It only:
  1. Opens signin.cloudbeds.com
  2. Enters email, clicks Next
  3. Enters password, clicks Next
  4. If 2FA prompted, switches to Authenticator factor + enters TOTP
  5. Waits for redirect to the Cloudbeds dashboard
  6. Prints the final URL + saves a screenshot of the landed page
"""
import asyncio
import os
import sys
import time
from pathlib import Path

# Ensure we can import the app from the backend root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


async def main() -> int:
    from app.config import settings
    from app.tools.cloudbeds_browser import CloudbedsBrowser, _current_totp

    print("Configuration check:")
    print(f"  login URL  : {settings.cloudbeds_login_url}")
    print(f"  email      : {settings.cloudbeds_admin_email or '(NOT SET)'}")
    print(f"  password   : {'SET' if settings.cloudbeds_admin_password else 'NOT SET'}")
    print(f"  TOTP secret: {'SET' if settings.cloudbeds_totp_secret else 'NOT SET'}")
    print(f"  current TOTP: {_current_totp() or 'N/A'}")
    print(f"  headless   : {settings.cloudbeds_browser_headless}")
    print()

    if not (settings.cloudbeds_admin_email and settings.cloudbeds_admin_password):
        print("ERROR: CLOUDBEDS_ADMIN_EMAIL and CLOUDBEDS_ADMIN_PASSWORD must be set in .env")
        return 1

    started = time.time()
    async with CloudbedsBrowser() as cb:
        ok = await cb.login()
        elapsed = time.time() - started
        if ok:
            print(f"\n[OK] Logged in after {elapsed:.1f}s")
            print(f"     Landed on: {cb.page.url}")
            shot = Path("logs") / f"login_success_{int(time.time())}.png"
            shot.parent.mkdir(exist_ok=True)
            await cb.page.screenshot(path=str(shot), full_page=True)
            print(f"     Screenshot: {shot}")
            print("\nPress Enter to close the browser...")
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                pass
            return 0
        else:
            print(f"\n[FAIL] Login failed after {elapsed:.1f}s")
            print(f"      Last URL: {cb.page.url if cb.page else 'n/a'}")
            print("      See backend/logs/ for a failure screenshot.")
            print("\nPress Enter to close the browser...")
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                pass
            return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
