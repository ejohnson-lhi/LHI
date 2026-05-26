"""Test: drive the Cloudbeds Add Card flow with full humanlike Playwright
automation, verify whether the Stripe Elements iframes accept our input.

This is the validation step before committing to a Playwright-driven
Add Card architecture for the guest portal + voice flows. Manual entry
from this environment works as of 2026-05-24 (the iframes accept your
OS-keyboard digits); whether Playwright-driven keystrokes work too is
the question this script answers.

Three possible outcomes:
  - It works end-to-end: the saved-card view shows our last-4. We have
    a clean architecture for both portal and voice flows.
  - Iframes silently swallow input (typing happens, no digits appear):
    Stripe detected automation. Pivot to one of the fallbacks
    (Stripe.js-with-Cloudbeds-key, or Pay-by-Link URL capture).
  - It works intermittently: the cadence still leaks; we tune harder or
    pivot. Use --runs N to retry N times in a row.

Usage:
    .venv\\Scripts\\python.exe scripts\\test_add_card_automation.py [reservation_id] [--dry-run] [--runs N]

Defaults:
    reservation_id = 1989264686165 (the one we walked through manually)
    no --dry-run  = will click Save and verify
    --runs        = 1

Card data MUST be provided via env vars (NEVER hardcode a PAN here, and
NEVER commit one — see project memory "Human cadence for card automation"):
    TEST_CARD_NAME       (default: "Test Card")
    TEST_CARD_PAN        (REQUIRED; digits only, no spaces)
    TEST_CARD_EXPIRY     ("MM/YY" or "MMYY", e.g. "12/30" or "1230")
    TEST_CARD_CVV        (3 digits for Visa/MC; 4 for Amex)

Example (Windows cmd):
    set TEST_CARD_PAN=4242424242424242
    set TEST_CARD_EXPIRY=1230
    set TEST_CARD_CVV=123
    set TEST_CARD_NAME=Test Card
    .venv\\Scripts\\python.exe scripts\\test_add_card_automation.py 1989264686165 --dry-run

Stripe test cards may or may not work against Cloudbeds Payments (live
gateway, not a sandbox). 4242 4242 4242 4242 is Stripe's standard test
Visa; if Cloudbeds rejects it the script will report the error message
from the form.

IMPORTANT: this script's trace.zip will contain DOM snapshots and
screenshots taken during typing -- treat it like CC data afterwards.
Delete logs/cloudbeds_trace_*.zip after a successful test if it
contained a real PAN.
"""
import asyncio
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

DEFAULT_RESERVATION_ID = "1989264686165"


def parse_args() -> tuple[str, bool, int]:
    args = list(sys.argv[1:])
    dry_run = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]
    runs = 1
    if "--runs" in args:
        i = args.index("--runs")
        if i + 1 < len(args):
            try:
                runs = max(1, int(args[i + 1]))
            except ValueError:
                runs = 1
            del args[i:i + 2]
    res_id = args[0] if args else DEFAULT_RESERVATION_ID
    return res_id, dry_run, runs


def normalize_expiry(s: str) -> str:
    s = (s or "").strip().replace(" ", "")
    if "/" in s:
        return s
    if len(s) == 4 and s.isdigit():
        return f"{s[:2]}/{s[2:]}"
    return s


def normalize_pan(s: str) -> str:
    return "".join(c for c in (s or "") if c.isdigit())


async def run_once(reservation_id: str, dry_run: bool, name: str, pan: str, exp: str, cvv: str) -> int:
    """Single attempt at the Add Card flow. Returns 0 on success, nonzero on failure."""
    from app.config import settings
    from app.tools.cloudbeds_browser import CloudbedsBrowser

    async with CloudbedsBrowser() as cb:
        ok = await cb.login()
        if not ok:
            print("[FAIL] Login did not complete.")
            return 1
        print("[OK] Logged in.")

        # 1. Confirm we're on the reservations list. login() already
        # navigated to _entry_url() (which IS the reservations URL), so
        # if that succeeded we're here -- no need to goto again. A real
        # human who bookmarked the reservations page wouldn't refresh
        # the same URL twice in a row; doing so would be a script tell.
        reservations_url = (
            f"https://hotels.cloudbeds.com/connect/{settings.cloudbeds_property_id}"
            "#/reservations"
        )
        if "#/reservations" in (cb.page.url or ""):
            print(f"\n[1/8] Already on the reservations list (looking at it for a beat)...")
            # Small "I'm reading the page" pause -- humans don't go from
            # bookmark-loaded to typing-into-search in 50ms.
            await asyncio.sleep(random.uniform(0.8, 1.6))
        else:
            print(f"\n[1/8] Landed somewhere else after login; navigating to reservations list...")
            await cb.page.goto(reservations_url, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(random.uniform(1.4, 2.0))

        # 2. Search for the reservation by number.
        print(f"[2/8] Typing reservation number into search (familiarity=1.0)...")
        ok = await cb._humanlike_type(
            reservation_id,
            "input[name='find_reservations']",
            timeout=10000,
            allow_typos=False,  # too short for the typo distribution to fit
            familiarity=1.0,
        )
        if not ok:
            print("[FAIL] Couldn't find the search input.")
            return 1
        # Submit search.
        await cb.page.locator("input[name='find_reservations']").press("Enter")
        await asyncio.sleep(random.uniform(1.8, 2.6))

        # 3. Click the reservation row. (Pause first -- a human reads the
        # result before clicking.)
        print(f"[3/8] Clicking the reservation row...")
        try:
            link = cb.page.locator("a.view_summary").first
            await link.wait_for(state="visible", timeout=10000)
            await asyncio.sleep(random.uniform(0.5, 1.1))
            await link.click()
            await asyncio.sleep(random.uniform(1.8, 2.6))
        except Exception as ex:
            print(f"[FAIL] Couldn't click the reservation row: {ex}")
            return 1

        # 4. Open Credit Cards tab.
        print(f"[4/8] Opening Credit Cards tab...")
        try:
            tab = cb.page.locator("a[href='#rs-credit-cards-tab-new']").first
            await tab.wait_for(state="visible", timeout=10000)
            await asyncio.sleep(random.uniform(0.35, 0.85))
            await tab.click()
            await asyncio.sleep(random.uniform(1.0, 1.6))
        except Exception as ex:
            print(f"[FAIL] Couldn't open Credit Cards tab: {ex}")
            return 1

        # 5. Click + Add Card.
        print(f"[5/8] Clicking + Add Card...")
        try:
            btn = cb.page.locator("button[data-hook='add-CCard']").first
            await btn.wait_for(state="visible", timeout=10000)
            await asyncio.sleep(random.uniform(0.5, 1.1))
            await btn.click()
            # Stripe iframes take a moment to mount.
            await asyncio.sleep(random.uniform(1.6, 2.4))
        except Exception as ex:
            print(f"[FAIL] Couldn't click + Add Card: {ex}")
            return 1

        # 6. Fill cardholder name. Own name -> familiarity 0.5 (fast bursts).
        print(f"[6/8] Typing cardholder name (familiarity=0.5)...")
        ok = await cb._humanlike_type(
            name,
            "input[name='cardholderName']",
            timeout=10000,
            allow_typos=True,  # name is long enough; mimics fat-finger rate
            familiarity=0.5,
        )
        if not ok:
            print("[FAIL] Couldn't type into cardholder name field.")
            return 1

        # 7. Fill the three Stripe iframes. We DON'T frame_locator into them
        # -- that caused scroll-jumping from Playwright's repeated actionability
        # checks fighting Stripe's own auto-scroll-on-focus. Instead we click
        # the page-level container (#cardNumber etc.); Stripe.js routes the
        # click into its iframe and focuses its input. Then page.keyboard.type
        # sends keystrokes to whatever has focus -- they land in the iframe
        # without us ever reaching into it.
        print(f"[7/8a] Typing PAN -- click #cardNumber container, then keyboard (familiarity=1.5)...")
        ok = await cb._humanlike_keyboard_into(
            cb.page.locator("#cardNumber").first, pan,
            allow_typos=False,    # never typo into a PAN
            familiarity=1.5,      # card-from-card cadence + look-back pauses
        )
        if not ok:
            print("[FAIL] Couldn't type PAN via container + keyboard.")
            return 2

        print(f"[7/8b] Typing expiration -- click .CardExpireContainer .StripeElement (familiarity=1.2)...")
        exp_digits = exp.replace("/", "")
        # The expire field's StripeElement div has an auto-gen ID (`:r6:`)
        # we can't rely on -- but .CardExpireContainer wraps it, and there
        # is only one .StripeElement inside, so this nested selector is
        # stable. (For PAN we have #cardNumber; for CVV we have #cardCvv;
        # only the expire field is unlucky enough to lack a stable id.)
        # break_after_chars=[2] forces a deliberate 400-900ms pause after
        # the month -- mimics the human "look at YY on the card" behavior
        # AND lets Stripe Elements finish its auto-"/"-insertion before
        # the next keystroke arrives (without it, the year is flaky).
        ok = await cb._humanlike_keyboard_into(
            cb.page.locator(".CardExpireContainer .StripeElement").first, exp_digits,
            allow_typos=False,
            familiarity=1.2,
            break_after_chars=[2],
        )
        if not ok:
            print("[FAIL] Couldn't type expiration via container + keyboard.")
            return 2

        print(f"[7/8c] Typing CVV -- click #cardCvv container (familiarity=1.2)...")
        ok = await cb._humanlike_keyboard_into(
            cb.page.locator("#cardCvv").first, cvv,
            allow_typos=False,
            familiarity=1.2,
        )
        if not ok:
            print("[FAIL] Couldn't type CVV via container + keyboard.")
            return 2

        # 8. Save (or stop here in dry-run).
        if dry_run:
            print(f"\n[8/8] DRY RUN: stopping BEFORE Save.")
            print(f"      Visually inspect the filled form. Browser stays open.")
            print(f"      Press Enter here to close.")
            try:
                await asyncio.get_event_loop().run_in_executor(None, input, "")
            except (EOFError, KeyboardInterrupt):
                pass
            return 0

        print(f"[8/8] Clicking Save...")
        # Pause first -- a human looks at the form before clicking Save.
        await asyncio.sleep(random.uniform(0.8, 1.6))
        save_btn = None
        # First entry is the captured Save selector (2026-05-25). The
        # outerHTML at that time was:
        #   <button type="button" class="btn blue btn-save">Save</button>
        # inside #panelSave. The remaining entries are defensive fallbacks
        # for if Cloudbeds rebuilds the form.
        for sel in [
            "#panelSave button.btn-save",
            "button.btn-save",
            "button[data-hook='credit-card-save']",
            "button[data-hook='save-CCard']",
            "button[data-action='save-card']",
            "button.btn-primary:has-text('Save')",
            "button:has-text('Save')",
        ]:
            try:
                cand = cb.page.locator(sel).first
                if await cand.is_visible():
                    save_btn = cand
                    print(f"        Using Save selector: {sel}")
                    break
            except Exception:
                continue
        if save_btn is None:
            print(f"[FAIL] Couldn't find the Save button. Please right-click it,")
            print(f"       Inspect, copy outerHTML, and paste back so I can wire it in.")
            print(f"       Browser stays open. Press Enter to close.")
            try:
                await asyncio.get_event_loop().run_in_executor(None, input, "")
            except (EOFError, KeyboardInterrupt):
                pass
            return 3
        await save_btn.click()
        # Give the save a moment to complete + the saved-card view to render.
        await asyncio.sleep(random.uniform(2.5, 4.0))

        # Verify: masked PAN in the saved-card view contains our last-4.
        try:
            masked = cb.page.locator("[data-hook='credit-card-number']").first
            await masked.wait_for(state="visible", timeout=10000)
            displayed = await masked.inner_text()
            expected_tail = pan[-4:]
            if expected_tail in displayed:
                print(f"\n[OK] Card saved successfully.")
                print(f"     Displayed: '{displayed}' (matches last-4 {expected_tail})")
                return 0
            else:
                print(f"\n[WARN] Save appears to have succeeded but masked PAN doesn't match.")
                print(f"       Expected last-4: {expected_tail}; displayed: '{displayed}'")
                return 4
        except Exception as ex:
            print(f"\n[WARN] Couldn't read the masked PAN to verify save: {ex}")
            print(f"       The saved-card view may not have rendered. Check manually.")
            return 5


async def main() -> int:
    from app.config import settings

    res_id, dry_run, runs = parse_args()

    name = os.environ.get("TEST_CARD_NAME", "Test Card")
    pan = normalize_pan(os.environ.get("TEST_CARD_PAN", ""))
    exp = normalize_expiry(os.environ.get("TEST_CARD_EXPIRY", ""))
    cvv = (os.environ.get("TEST_CARD_CVV") or "").strip()

    missing = [k for k, v in [("TEST_CARD_PAN", pan), ("TEST_CARD_EXPIRY", exp), ("TEST_CARD_CVV", cvv)] if not v]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}")
        print(f"Set them in your shell before running. See top of file for examples.")
        return 1

    if settings.cloudbeds_browser_headless:
        print("ERROR: CLOUDBEDS_BROWSER_HEADLESS=true in your .env.")
        print("       This test needs headed mode so you can see what happens.")
        return 1

    print("=" * 72)
    print(f"Test: Add Card automation against reservation {res_id}")
    print("=" * 72)
    print(f"Mode             : {'DRY RUN (stops before Save)' if dry_run else 'LIVE (will click Save)'}")
    print(f"Runs             : {runs}")
    print(f"Card name        : {name}")
    print(f"PAN (last 4)     : ...{pan[-4:]}")
    print(f"Expiration       : {exp}")
    print(f"CVV              : (set, {len(cvv)} chars)")
    print(f"slow_mo_ms       : {settings.cloudbeds_browser_slow_mo_ms}")
    print(f"typing_delay_ms  : {settings.cloudbeds_typing_delay_ms} (base; mixture-distributed per char)")
    print(f"action_pause_ms  : {settings.cloudbeds_action_pause_ms}")
    print()

    results: list[int] = []
    for i in range(runs):
        if runs > 1:
            print(f"\n--- Run {i + 1}/{runs} ---")
        rc = await run_once(res_id, dry_run, name, pan, exp, cvv)
        results.append(rc)
        if runs > 1 and i + 1 < runs:
            # Inter-run gap so we don't look like a script hammering Cloudbeds.
            gap = random.uniform(45, 90)
            print(f"\n(sleeping {gap:.1f}s before next run...)")
            await asyncio.sleep(gap)

    if runs > 1:
        print(f"\n--- Summary ---")
        good = sum(1 for r in results if r == 0)
        print(f"Successful runs: {good}/{runs}")
        for i, r in enumerate(results, 1):
            status = "OK" if r == 0 else f"FAIL (code {r})"
            print(f"  Run {i}: {status}")
    return 0 if all(r == 0 for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
