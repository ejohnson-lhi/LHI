"""Dump the raw Cloudbeds getReservation response (NOT summarized) so we
can see every field and identify which one carries the internal booking
ID (e.g. 175931510 for Test1 Case).

We need this mapping to call dashboard_save_credit_card, which requires
the internal booking_id rather than the public reservationID.

If the public API exposes a `bookingID` (or similar) field, we can do
the lookup with a single public-API call. If not, we'll need to use the
dashboard's internal search endpoint instead.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

backend_root = Path(__file__).parent.parent
os.chdir(backend_root)
sys.path.insert(0, str(backend_root))

from app.tools.cloudbeds import _get  # noqa: E402

DEFAULT_RES_ID = "1989264686165"


async def main() -> int:
    res_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_RES_ID
    print(f"Fetching getReservation for {res_id}...")
    print()

    body = await _get("getReservation", params={"reservationID": res_id})
    if not body:
        print("ERROR: empty response from Cloudbeds API.")
        return 1

    data = body.get("data") or {}
    if not isinstance(data, dict):
        print("ERROR: unexpected response shape:")
        print(json.dumps(body, indent=2, default=str)[:1500])
        return 1

    print("Top-level fields in `data`:")
    for k in sorted(data.keys()):
        v = data[k]
        if isinstance(v, (list, dict)):
            preview = f"{type(v).__name__}({len(v)})"
        else:
            s = str(v)
            preview = s[:60] + ("..." if len(s) > 60 else "")
        print(f"  {k:30s} = {preview}")

    print()
    # Highlight fields that look like they might be the internal booking id
    print("Likely booking-id candidates (any field with 'booking' or 'id' in the name):")
    found_candidate = False
    for k, v in data.items():
        if "booking" in k.lower() or k.lower().endswith("id"):
            print(f"  {k} = {v!r}")
            found_candidate = True
    if not found_candidate:
        print("  (none found in top-level)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
