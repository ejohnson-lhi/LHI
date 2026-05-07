"""Diagnostic: try creating reservations with sourceIDs s-1..s-12 to see
which ones Cloudbeds postReservation accepts.

Run from backend dir:
    python scripts/test_source_ids.py
"""
import asyncio
import os
import sys
from datetime import date, timedelta
from pathlib import Path

backend_root = Path(__file__).parent.parent
os.chdir(backend_root)
sys.path.insert(0, str(backend_root))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402

PROPERTY_ID = settings.cloudbeds_property_id
ROOM_TYPE_ID = "240375"  # King
BASE_DATE = date(2026, 6, 10)


async def try_source(client: httpx.AsyncClient, source_id: str, day_offset: int) -> tuple[str, bool, str, str]:
    check_in = BASE_DATE + timedelta(days=day_offset)
    check_out = check_in + timedelta(days=1)
    form = {
        "propertyID": PROPERTY_ID,
        "startDate": check_in.isoformat(),
        "endDate": check_out.isoformat(),
        "guestFirstName": "ZZZ",
        "guestLastName": f"SrcTest-{source_id}",
        "guestEmail": "none@test.com",
        "guestCountry": "US",
        "guestZip": "",
        "rooms[0][roomTypeID]": ROOM_TYPE_ID,
        "rooms[0][quantity]": "1",
        "adults[0][roomTypeID]": ROOM_TYPE_ID,
        "adults[0][quantity]": "2",
        "children[0][roomTypeID]": ROOM_TYPE_ID,
        "children[0][quantity]": "0",
        "sendEmailConfirmation": "false",
        "paymentMethod": "cash",
        "sourceID": source_id,
    }
    try:
        r = await client.post(
            "https://api.cloudbeds.com/api/v1.3/postReservation",
            data=form,
            headers={"Authorization": f"Bearer {settings.cloudbeds_api_key}"},
        )
        body = r.json()
        ok = bool(body.get("success", False))
        rid = body.get("reservationID", "") or ""
        msg = body.get("message", "") or ""
        return source_id, ok, rid, msg
    except Exception as e:
        return source_id, False, "", f"exception: {e}"


async def main() -> None:
    if not settings.cloudbeds_api_key:
        print("ERROR: CLOUDBEDS_API_KEY not configured")
        sys.exit(1)
    async with httpx.AsyncClient(timeout=20.0) as client:
        results = []
        for i in range(1, 13):
            sid = f"s-{i}"
            results.append(await try_source(client, sid, i - 1))

    print(f"{'sourceID':<10} {'success':<10} {'reservationID':<16} message")
    print("-" * 70)
    for sid, ok, rid, msg in results:
        print(f"{sid:<10} {str(ok):<10} {rid:<16} {msg[:50]}")


if __name__ == "__main__":
    asyncio.run(main())
