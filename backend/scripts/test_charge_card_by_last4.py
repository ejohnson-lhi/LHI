"""Charge a card on a Cloudbeds reservation, identifying it by last4.

The user-facing interface uses things they know -- reservation ID and the
last 4 digits of the card -- and resolves those to the internal
booking_id + credit_card_id the dashboard endpoint needs.

Defaults to DRY-RUN: prints what would be charged, returns success
without touching the dashboard. To actually fire the charge:

    set CONFIRM_REAL_CHARGE=yes-charge-the-card
    .venv\\Scripts\\python.exe scripts\\test_charge_card_by_last4.py \\
        --reservation 6028215545560 --last4 9395 --amount 162.00 \\
        --capture

Belt-and-suspenders: BOTH `--capture` AND the env var are required, and
the env var phrase is verbatim. Either alone is a no-op (with a clear
error message), so accidentally running with --capture from history
won't move money.

Without --capture (default), the script runs the resolution step,
prints what it found, and exits 0 -- useful as a sanity check before
the real run.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

_CAPTURE_CONFIRM_PHRASE = "yes-charge-the-card"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--reservation", required=True,
                   help="Cloudbeds public reservation ID (e.g. 6028215545560)")
    p.add_argument("--last4", required=True,
                   help="Last 4 digits of the card to charge (must match a "
                        "cardNumber on the reservation's cards_on_file)")
    p.add_argument("--amount", required=True, type=float,
                   help="Dollar amount to charge (e.g. 162.00)")
    p.add_argument("--description", default="Iris test charge",
                   help="Folio line description (default: 'Iris test charge')")
    p.add_argument("--capture", action="store_true",
                   help="Actually fire the charge. Requires "
                        f"CONFIRM_REAL_CHARGE={_CAPTURE_CONFIRM_PHRASE}")
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    # Import inside main so an arg-parse failure doesn't pay the import cost.
    from app.tools.cloudbeds import get_reservation_by_id  # noqa: PLC0415
    from app.tools.cloudbeds_dashboard import (  # noqa: PLC0415
        dashboard_capture_card, get_booking_id,
    )

    # --- Step 1: load the reservation ---------------------------------
    print(f"Looking up reservation {args.reservation}...")
    res = await get_reservation_by_id(args.reservation)
    if res is None:
        print(f"ERROR: reservation {args.reservation!r} not found.")
        return 2

    guest = res.get("guest_name") or "(unknown guest)"
    check_in = res.get("check_in") or "?"
    check_out = res.get("check_out") or "?"
    status = res.get("status") or "?"
    cards = res.get("cards_on_file") or []
    print(f"  guest:     {guest}")
    print(f"  check-in:  {check_in}")
    print(f"  check-out: {check_out}")
    print(f"  status:    {status}")
    print(f"  cards on file: {len(cards)}")
    for i, c in enumerate(cards, start=1):
        # cardNumber is the last4 in this response shape.
        last4 = c.get("cardNumber") or "????"
        brand = c.get("cardType") or "(unknown brand)"
        card_id = c.get("cardID") or "(no id)"
        marker = "  <-- match" if last4 == args.last4 else ""
        print(f"    {i}. {brand:12} ****{last4}  cardID={card_id}{marker}")

    # --- Step 2: pick the card by last4 -------------------------------
    matching = [c for c in cards if (c.get("cardNumber") or "") == args.last4]
    if not matching:
        print(f"ERROR: no card with last4={args.last4!r} found on reservation.")
        return 3
    if len(matching) > 1:
        print(f"ERROR: {len(matching)} cards match last4={args.last4!r}; "
              "this is ambiguous. Specify a different card or pre-resolve "
              "the cardID manually.")
        return 3
    chosen = matching[0]
    credit_card_id = str(chosen.get("cardID") or "").strip()
    if not credit_card_id:
        print("ERROR: matched card has no cardID; can't charge it.")
        return 3

    # --- Step 3: resolve booking_id ----------------------------------
    print(f"Resolving booking_id for reservation {args.reservation}...")
    booking_id = await get_booking_id(args.reservation)
    if not booking_id:
        print("ERROR: couldn't resolve booking_id. Possible causes: "
              "dashboard session expired, reservation not visible to this "
              "property's session, or the search endpoint changed.")
        return 4
    print(f"  booking_id: {booking_id}")

    # --- Step 4: charge preview --------------------------------------
    print()
    print("=" * 60)
    print("CHARGE PREVIEW")
    print("=" * 60)
    print(f"  Guest:          {guest}")
    print(f"  Reservation:    {args.reservation}  (booking_id={booking_id})")
    print(f"  Card:           {chosen.get('cardType') or '?'} ****{args.last4}  "
          f"(cardID={credit_card_id})")
    print(f"  Amount:         ${args.amount:,.2f}")
    print(f"  Description:    {args.description}")
    print(f"  Operation:      {'CAPTURE (real charge)' if args.capture else 'DRY RUN'}")
    print("=" * 60)
    print()

    if not args.capture:
        print("Dry run -- nothing was charged. Re-run with --capture and")
        print(f"           CONFIRM_REAL_CHARGE={_CAPTURE_CONFIRM_PHRASE}")
        print("           to fire the actual charge.")
        return 0

    # --- Step 5: confirmation gate -----------------------------------
    confirm = (os.environ.get("CONFIRM_REAL_CHARGE") or "").strip()
    if confirm != _CAPTURE_CONFIRM_PHRASE:
        print(f"ERROR: --capture passed but CONFIRM_REAL_CHARGE env var is not")
        print(f"       set to the required phrase ({_CAPTURE_CONFIRM_PHRASE!r}).")
        print(f"       Refusing to charge ${args.amount:,.2f} without explicit confirmation.")
        return 5

    # --- Step 6: fire the capture ------------------------------------
    print(f"Firing capture for ${args.amount:,.2f}...")
    result = await dashboard_capture_card(
        booking_id=booking_id,
        credit_card_id=credit_card_id,
        amount=args.amount,
        description=args.description,
    )
    print()
    print("CAPTURE RESPONSE:")
    print(json.dumps(result, indent=2, default=str))
    print()
    if result.get("success"):
        print(f"SUCCESS. Captured ${args.amount:,.2f} on card ****{args.last4}.")
        return 0
    print(f"FAILED. See response above for details.")
    return 6


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
