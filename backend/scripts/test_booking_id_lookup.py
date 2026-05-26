"""Test the public-reservation-id → internal-booking-id lookup.

Tries the fast path (extract booking_id from a card-on-file) first; if
no cards are attached, falls back to a Playwright-driven dashboard
search. Either way, prints the resolved booking_id.

Usage:
    .venv\\Scripts\\python.exe scripts\\test_booking_id_lookup.py [reservation_id]

Default reservation: 1989264686165 (Test1 Case, currently has 7 cards on
file from prior tests — so fast path should win).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

DEFAULT_RESERVATION_ID = "1989264686165"


async def main() -> int:
    from app.tools.cloudbeds_dashboard import get_booking_id

    res_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_RESERVATION_ID
    print(f"Reservation ID (public): {res_id}")
    print()
    print("Resolving via dashboard search (Playwright, ~10-15s)...")
    print()

    bid = await get_booking_id(res_id)
    print()
    if bid:
        print(f"[OK] booking_id = {bid}")
        return 0
    print("[FAIL] No booking_id resolved. Check Playwright login + search selectors.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
