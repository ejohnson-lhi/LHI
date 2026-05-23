"""One-off diagnostic: call post_credit_card via the paymentMethodId path
with an obviously-invalid PM ID, to distinguish two failure modes for
the previous raw-card tests:

  1. API-key scope issue (Payment-Write missing) — Cloudbeds returns the
     same generic "An unexpected error occurred" regardless of payload.
  2. Raw-card path specifically blocked but tokenized path works — Cloudbeds
     accepts the request structurally, tries to look up the PM ID, and
     returns a specific "no such payment method" / "invalid token" error.

Run from backend dir:
    .venv\\Scripts\\python scripts\\test_post_credit_card_pmid.py

Or override with a real PM ID (e.g. one created via Stripe Elements on the
property's Connect account acct_1RYA4xEJ572tmEoR):
    PM_ID=pm_1ABC... .venv\\Scripts\\python scripts\\test_post_credit_card_pmid.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

backend_root = Path(__file__).parent.parent
os.chdir(backend_root)
sys.path.insert(0, str(backend_root))

from app.tools.cloudbeds import post_credit_card  # noqa: E402

RESERVATION_ID = "1989264686165"   # Test1 Case
PM_ID          = os.environ.get("PM_ID", "pm_obviouslyFakeForDiagnostic1234567890")


async def main() -> int:
    print(f"Target reservation: {RESERVATION_ID}")
    print(f"PaymentMethod ID:   {PM_ID}")
    print()
    print("Calling post_credit_card(payment_method_id=...) ...")

    result = await post_credit_card(
        RESERVATION_ID,
        payment_method_id=PM_ID,
    )

    print()
    print("Result:")
    for k, v in result.items():
        print(f"  {k}: {v}")

    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
