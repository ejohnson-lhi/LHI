"""Test the dashboard's internal `save_credit_card` endpoint with a
Stripe legacy token created via the browser test page.

This is the validation step before we wire `dashboard_save_credit_card`
into the actual guest portal. If this test succeeds, we have an
httpx-only, ~1-2s, no-Playwright, PCI-clean card-capture path.

Pre-requisites:
  1. A valid Cloudbeds session cached at backend/data/.cloudbeds_session.json
     (run scripts/test_cloudbeds_login.py first if missing or > 8h old).
  2. A Stripe legacy token + its card sub-object, produced via the updated
     scripts/stripe_tokenize_test.html page.

Usage (from backend/):
    set TEST_BOOKING_ID=175931510   :: Test1 Case (default)
    set TEST_TOKEN_ID=tok_xxx
    set TEST_TOKEN_CARD_JSON={"id":"card_xxx","brand":"Visa","exp_month":10,...}
    .venv\\Scripts\\python.exe scripts\\test_dashboard_save_card.py

The browser test page provides a "Copy env vars" button that puts a
ready-to-paste block onto your clipboard.

After running, check that the card appears in Cloudbeds (Reservations ->
Test1 Case -> Credit Cards tab). If it does, this architecture is
validated. Three cards from prior tests are already on this reservation;
you may want to deactivate the older ones.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

DEFAULT_BOOKING_ID = "175931510"


async def main() -> int:
    from app.tools.cloudbeds_dashboard import dashboard_save_credit_card

    booking_id = os.environ.get("TEST_BOOKING_ID", DEFAULT_BOOKING_ID).strip()
    token_id = os.environ.get("TEST_TOKEN_ID", "").strip()
    card_json = os.environ.get("TEST_TOKEN_CARD_JSON", "").strip()

    if not token_id:
        print("ERROR: missing TEST_TOKEN_ID env var.")
        print("       Generate it via stripe_tokenize_test.html (the 'tok_xxx' value).")
        return 1
    if not card_json:
        print("ERROR: missing TEST_TOKEN_CARD_JSON env var.")
        print("       Generate it via stripe_tokenize_test.html (the JSON card object).")
        return 1

    try:
        token_card = json.loads(card_json)
    except json.JSONDecodeError as e:
        print(f"ERROR: TEST_TOKEN_CARD_JSON is not valid JSON: {e}")
        print(f"       Got: {card_json[:200]}")
        return 1

    print(f"Booking ID  : {booking_id}")
    print(f"Token ID    : {token_id}")
    print(f"Card last4  : {token_card.get('last4')}")
    print(f"Card brand  : {token_card.get('brand')}")
    print(f"Card name   : {token_card.get('name')}")
    print(f"Card exp    : {token_card.get('exp_month')}/{token_card.get('exp_year')}")
    print()
    print("Calling dashboard_save_credit_card via internal endpoint...")
    print()

    result = await dashboard_save_credit_card(
        booking_id=booking_id,
        legacy_token_id=token_id,
        token_card=token_card,
    )

    print("Result:")
    print(json.dumps(result, indent=2, default=str)[:2000])
    print()

    if result.get("success"):
        print("[OK] Card saved via dashboard endpoint.")
        print("     Verify on the reservation's Credit Cards tab in Cloudbeds.")
        return 0
    else:
        print("[FAIL]", result.get("error"))
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
