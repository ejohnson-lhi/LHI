"""Phase 2: discover the selectors needed to generate a Pay-by-Link via
the Cloudbeds dashboard.

Usage:
    .venv/Scripts/python.exe scripts/discover_paylink_selectors.py <reservation_id>

What it does:
  1. Logs in (re-using cached session if available).
  2. Navigates to https://hotels.cloudbeds.com/connect/176010 .
  3. Opens Playwright's Inspector so YOU can drive the browser while I see
     what you click. The Inspector window has a "Record" button -- when
     you click it, every interaction gets translated to Playwright
     selectors that show up in real time.
  4. While you click through "Send Pay-by-Link" (or whatever it's named):
     - At each step, look at the Inspector's bottom pane: it shows the
       selector Playwright thinks best matches what you just clicked.
     - Copy + paste those selectors back to me when done.
  5. Browser stays open until you Ctrl+C in the terminal.

What I'm looking for (paste these to me):
  - Selector for the "Send Pay-by-Link" / "Send Payment Request" button
    on the reservation page
  - Selector for the amount input field
  - Selector for the "Authorization only" / "Auth-only" / "Hold" toggle
    (if it exists -- there might not be one, in which case we set amount
    to $0.01 instead)
  - Selector for the "Generate" / "Send" / "Create Link" button
  - Where the generated URL appears (textbox? copyable link?) and how to
    grab it
"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


async def main(reservation_id: str) -> int:
    from app.tools.cloudbeds_browser import CloudbedsBrowser
    from app.config import settings

    print(f"Logging in (re-using cached session if possible)...")
    print(f"Target reservation: {reservation_id}")
    print()

    async with CloudbedsBrowser() as cb:
        ok = await cb.login()
        if not ok:
            print("[FAIL] Login failed -- see backend/logs/ for screenshots.")
            return 1
        print(f"[OK] Logged in. URL: {cb.page.url}")

        # Try the obvious URL pattern first; we'll see if Cloudbeds
        # supports deep-linking by reservation ID.
        target = f"https://hotels.cloudbeds.com/connect/{settings.cloudbeds_property_id}/reservations/{reservation_id}"
        print(f"\nNavigating to: {target}")
        try:
            await cb.page.goto(target, wait_until="domcontentloaded", timeout=20000)
        except Exception as ex:
            print(f"  Direct navigation failed ({ex}); falling back to dashboard root.")
            await cb.page.goto(
                f"https://hotels.cloudbeds.com/connect/{settings.cloudbeds_property_id}",
                wait_until="domcontentloaded", timeout=20000,
            )

        print(f"\nLanded on: {cb.page.url}")
        print()
        print("=" * 70)
        print("BROWSER IS NOW YOURS.")
        print("=" * 70)
        print()
        print("Walk through these steps. After each click, look at the Inspector")
        print("window for the suggested selector and SAVE IT for me:")
        print()
        print("  1. Navigate to a reservation if you're not on one already.")
        print("     Note the URL pattern.")
        print()
        print("  2. Find the 'Send Pay-by-Link' button (it might be in a menu,")
        print("     a sidebar, or under an 'Actions' dropdown).")
        print("     >>> RIGHT-CLICK that button -> Inspect -> copy outerHTML")
        print()
        print("  3. Click it. A form should appear (modal, sidebar, or new page).")
        print()
        print("  4. Find the AMOUNT input. >>> Right-click -> Inspect -> outerHTML")
        print()
        print("  5. Look for an 'Auth-only' / 'Authorization only' / 'Hold' toggle.")
        print("     If you find one, >>> outerHTML it. If not, no problem -- we'll")
        print("     use $0.01 instead.")
        print()
        print("  6. Click the 'Generate' / 'Send' button.")
        print("     >>> Right-click that button -> Inspect -> outerHTML")
        print()
        print("  7. The generated URL should appear somewhere on the page.")
        print("     >>> Right-click the URL element -> Inspect -> outerHTML")
        print()
        print("Browser stays open until you press Enter (or Ctrl+C) here.")
        print()
        try:
            await asyncio.get_event_loop().run_in_executor(None, input, "")
        except (EOFError, KeyboardInterrupt):
            pass

    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: discover_paylink_selectors.py <reservation_id>")
        print()
        print("Use any active reservation ID from your live data. The script just")
        print("opens the dashboard at that reservation so you can walk through")
        print("the Pay-by-Link flow.")
        sys.exit(1)
    raise SystemExit(asyncio.run(main(sys.argv[1])))
