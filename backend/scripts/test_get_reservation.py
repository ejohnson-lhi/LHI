"""Quick reservation lookup — sanity check before the post_credit_card test.

Run from backend dir:
    .venv\\Scripts\\python scripts\\test_get_reservation.py
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

from app.tools.cloudbeds import get_reservation_by_id  # noqa: E402

RESERVATION_ID = "1989264686165"


async def main() -> int:
    res = await get_reservation_by_id(RESERVATION_ID)
    if res is None:
        print(f"NOT FOUND: reservation {RESERVATION_ID}")
        return 1
    print(json.dumps(res, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
