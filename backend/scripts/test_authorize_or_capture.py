"""Test the Authorize and Capture flows on a saved card.

By default this script does an AUTHORIZE (places a $1 hold — virtually
free, expires ~7 days at the bank if not captured). To do a real CAPTURE
(actual charge), you must pass --capture AND set the env var
CONFIRM_REAL_CHARGE=yes-charge-the-card so it's impossible to fire a
real charge by accident.

Usage:
    # Safe: $1 hold on the saved card we tested with
    .venv\\Scripts\\python.exe scripts\\test_authorize_or_capture.py

    # Real charge ($1 default):
    set CONFIRM_REAL_CHARGE=yes-charge-the-card
    .venv\\Scripts\\python.exe scripts\\test_authorize_or_capture.py --capture

    # Override amount/booking/card:
    set TEST_AMOUNT=2.50
    set TEST_BOOKING_ID=175931510
    set TEST_CREDIT_CARD_ID=94390739
    .venv\\Scripts\\python.exe scripts\\test_authorize_or_capture.py

Defaults:
    TEST_BOOKING_ID       175931510  (Test1 Case)
    TEST_CREDIT_CARD_ID   94390739   (Visa 4958 saved via our portal flow)
    TEST_AMOUNT           1.00       (small to minimize real-money impact)
    TEST_DESCRIPTION      "Iris test transaction"
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

DEFAULT_BOOKING_ID = "175931510"
DEFAULT_CREDIT_CARD_ID = "94390739"
DEFAULT_AMOUNT = "1.00"
DEFAULT_DESCRIPTION = "Iris test transaction"

_CAPTURE_CONFIRM_PHRASE = "yes-charge-the-card"


def parse_args() -> bool:
    """Returns True if --capture was passed."""
    return "--capture" in sys.argv[1:]


async def main() -> int:
    from app.tools.cloudbeds_dashboard import (
        dashboard_authorize_card,
        dashboard_capture_card,
    )

    do_capture = parse_args()
    booking_id = os.environ.get("TEST_BOOKING_ID", DEFAULT_BOOKING_ID).strip()
    credit_card_id = os.environ.get("TEST_CREDIT_CARD_ID", DEFAULT_CREDIT_CARD_ID).strip()
    amount_str = os.environ.get("TEST_AMOUNT", DEFAULT_AMOUNT).strip()
    description = os.environ.get("TEST_DESCRIPTION", DEFAULT_DESCRIPTION)

    try:
        amount = float(amount_str)
    except ValueError:
        print(f"ERROR: TEST_AMOUNT={amount_str!r} is not a valid float.")
        return 1

    if do_capture:
        confirm = os.environ.get("CONFIRM_REAL_CHARGE", "").strip()
        if confirm != _CAPTURE_CONFIRM_PHRASE:
            print(f"ERROR: --capture would charge a real card ${amount:.2f}.")
            print(f"       To proceed, also set:")
            print(f"           CONFIRM_REAL_CHARGE={_CAPTURE_CONFIRM_PHRASE}")
            return 1
        op_name = "Capture (REAL CHARGE)"
        fn = dashboard_capture_card
    else:
        op_name = "Authorize (hold, no money moves)"
        fn = dashboard_authorize_card

    print("="*68)
    print(f"Operation     : {op_name}")
    print(f"Booking ID    : {booking_id}")
    print(f"Credit Card ID: {credit_card_id}")
    print(f"Amount        : ${amount:.2f}")
    print(f"Description   : {description}")
    print("="*68)
    print()

    result = await fn(
        booking_id=booking_id,
        credit_card_id=credit_card_id,
        amount=amount,
        description=description,
    )

    print("Result:")
    print(json.dumps(result, indent=2, default=str)[:2000])
    print()

    if not result.get("success"):
        print("[FAIL]", result.get("error"))
        return 1

    if result.get("requires_authentication"):
        print("[3DS REQUIRED] The issuer needs extra verification.")
        print(f"    Redirect the guest to: {result['authentication_url']}")
        print("    After they complete the challenge, the transaction settles.")
        return 0

    if do_capture:
        print(f"[OK] Charged ${result.get('paid')}. transaction_id={result.get('transaction_id')}")
    else:
        print(f"[OK] Authorized ${result.get('amount')}. status={result.get('status')}")
        print(f"     Hold expires automatically at the issuer (typically ~7 days).")
    print(f"     Payment row id={result.get('payment_id')} on reservation booking_id={booking_id}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
