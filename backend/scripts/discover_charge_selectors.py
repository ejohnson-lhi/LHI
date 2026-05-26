"""Track a manual walkthrough of the Cloudbeds charge / authorize flow.

You drive the browser; we record everything via Playwright's trace + a
screenshot at the moment you press Enter. The point is to surface the
exact selectors (button names, input IDs, modal containers) we'll need
when we automate the same flow later, AND to confirm whether your account
+ IP can actually type into the Stripe Elements iframe by hand right now.

Usage (from backend/):
    .venv\\Scripts\\python.exe scripts\\discover_charge_selectors.py [reservation_id]

Defaults to reservation 1989264686165 when no id is passed.

Required env (already in .env if Cloudbeds login is working):
    CLOUDBEDS_LOGIN_URL
    CLOUDBEDS_ADMIN_EMAIL
    CLOUDBEDS_ADMIN_PASSWORD
    CLOUDBEDS_TOTP_SECRET
    CLOUDBEDS_PROPERTY_ID

Strongly recommended:
    CLOUDBEDS_BROWSER_HEADLESS=false   (must be false; you need to see the browser)
    CLOUDBEDS_BROWSER_SLOW_MO_MS=80    (default; keeps the bot less obviously a bot)

What happens:
  1. Chromium opens, headed, with humanlike anti-detection flags (the
     existing CloudbedsBrowser context handles this).
  2. You're logged in to Cloudbeds (cached session reused when present).
  3. The script navigates to the reservation page.
  4. CONTROL IS YOURS. Walk through "Add Card" / "Charge" / "Authorize"
     exactly the way a human would. **Slow down.** Don't click fast,
     don't paste, don't tab through fields at machine speed. The flow
     of interest is the actual card-entry — if it works manually now,
     it tells us a lot about whether automation will work soon.
  5. At each meaningful step (modal opens, PAN field accepts input,
     submit succeeds/fails), pause and:
        - Right-click on the element of interest -> Inspect
        - Copy the outerHTML of that node
        - Paste it back in chat
  6. When you're done OR the flow has clearly hit a wall, press Enter
     in this terminal. The browser closes and the trace.zip writes out.

Artifacts written:
  - backend/logs/cloudbeds_trace_<ts>.zip   (full Playwright trace; open
    with: playwright show-trace <path>)
  - backend/logs/cloudbeds_*.png            (any failure screenshots)

What I'm trying to identify from the outerHTML you paste:
  - The button / menu item that opens the charge/authorize panel
  - PAN input, expiry (MM/YY), CVC, cardholder name field selectors
  - The charge-vs-authorize toggle (radio, dropdown, separate buttons?)
  - The amount input + currency selector if any
  - The submit button + where success / error text shows up
  - Whether the panel is a modal in the page, a separate sub-frame, or
    an iframe (this matters a lot for Playwright targeting)
"""
import asyncio
import os
import sys
from pathlib import Path

# Make `from app...` imports work when running this from anywhere.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

DEFAULT_RESERVATION_ID = "1989264686165"


async def main(reservation_id: str) -> int:
    from app.config import settings
    from app.tools.cloudbeds_browser import CloudbedsBrowser

    if settings.cloudbeds_browser_headless:
        print("WARNING: CLOUDBEDS_BROWSER_HEADLESS=true in .env.")
        print("         This script needs headed mode -- the whole point is for")
        print("         YOU to drive the browser. Set CLOUDBEDS_BROWSER_HEADLESS=false")
        print("         and re-run.")
        return 1

    print(f"Target reservation: {reservation_id}")
    print(f"slow_mo_ms        : {settings.cloudbeds_browser_slow_mo_ms} "
          f"(per-Playwright-action delay)")
    print(f"typing_delay_ms   : {settings.cloudbeds_typing_delay_ms} "
          f"(base; jittered 0.5x-1.5x per char on automated input)")
    print()
    print("Logging in to Cloudbeds (re-using cached session if available)...")
    print()

    async with CloudbedsBrowser() as cb:
        ok = await cb.login()
        if not ok:
            print("[FAIL] Login did not complete. Check backend/logs/ for screenshots.")
            return 1
        print(f"[OK] Logged in. URL: {cb.page.url}")

        # Land on the reservations list (the human path) rather than deep-link
        # to a reservation. Cloudbeds uses hash routing; the URL ID inside
        # /#/reservations/{n} is NOT the public reservation number, so search
        # by number is the reliable way in. We stop here -- the human does
        # the search + click themselves, so the eventual selectors come from
        # real user interaction with no automation contaminating the trace.
        reservations_url = (
            f"https://hotels.cloudbeds.com/connect/{settings.cloudbeds_property_id}"
            "#/reservations"
        )
        print(f"\nNavigating to reservations list: {reservations_url}")
        try:
            await cb.page.goto(reservations_url, wait_until="domcontentloaded", timeout=20000)
            # Let the hash-routed SPA finish hydrating before handing over.
            await asyncio.sleep(1.5)
        except Exception as ex:
            print(f"  Navigation failed ({ex}); falling back to dashboard home.")
            await cb.page.goto(
                f"https://hotels.cloudbeds.com/connect/{settings.cloudbeds_property_id}",
                wait_until="domcontentloaded", timeout=20000,
            )

        print(f"Landed on: {cb.page.url}")
        print()
        print("=" * 72)
        print(f"CONTROL IS YOURS. Walk through Add Card for reservation {reservation_id}.")
        print("=" * 72)
        print()
        print("HOW TO PROCEED")
        print("--------------")
        print(f"  1. In the search box (name='find_reservations'), type the")
        print(f"     reservation number ({reservation_id}) and press Enter.")
        print(f"  2. Click the resulting reservation row.")
        print(f"  3. Click the 'Credit Cards' tab.")
        print(f"  4. Click '+ Add Card'.")
        print(f"  5. Fill in Name, Card Number, Expiration, CVV. **Type slowly**")
        print(f"     into each field as a human would. The PAN/exp/CVV are")
        print(f"     Stripe Elements iframes -- they're the most likely thing")
        print(f"     to silently refuse keystrokes.")
        print(f"  6. Click Save.")
        print()
        print("KEY DIAGNOSTIC -- TELL ME AFTERWARDS:")
        print("  - Did the Card Number iframe accept your keystrokes? (digits visible?)")
        print("  - Did Expiration / CVV accept yours?")
        print("  - Did Save succeed? Any error message? (paste its outerHTML)")
        print("  - What did the success indicator look like? (paste its outerHTML)")
        print()
        print("Trace + screenshots auto-saved. Replay later with:")
        print("  playwright show-trace backend/logs/cloudbeds_trace_<timestamp>.zip")
        print()
        print("When you're done OR hit a wall, press Enter here. The browser closes.")
        print()

        try:
            await asyncio.get_event_loop().run_in_executor(
                None, input, "Press Enter to close the browser... "
            )
        except (EOFError, KeyboardInterrupt):
            pass

    print()
    print("Browser closed. Check backend/logs/ for the trace.zip and any screenshots.")
    return 0


if __name__ == "__main__":
    res_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_RESERVATION_ID
    raise SystemExit(asyncio.run(main(res_id)))
