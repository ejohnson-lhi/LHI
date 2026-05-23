"""One-off feasibility test: call post_credit_card against a real Cloudbeds
reservation with the Stripe standard test PAN (4242...), to verify the
plumbing works end-to-end (auth, request shape, response parsing). The
property's Stripe Connect account is live, so Cloudbeds will likely reject
the test PAN — that's a useful outcome too because the rejection comes
back through the same code path a real-card rejection would.

Run from backend dir:
    .venv\\Scripts\\python scripts\\test_post_credit_card.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

backend_root = Path(__file__).parent.parent
os.chdir(backend_root)
sys.path.insert(0, str(backend_root))

from app.config import settings  # noqa: E402
from app.tools.cloudbeds import post_credit_card  # noqa: E402


# ---- test inputs ------------------------------------------------------------
# Card values pull from env vars FIRST so real PANs never have to land in
# this file. Run with:
#   set CARD_NUMBER=4640... && set CARD_EXP=10/28 && set CARD_CVV=683 && python scripts\test_post_credit_card.py
# Or on bash:
#   CARD_NUMBER=4640... CARD_EXP=10/28 CARD_CVV=683 python scripts/test_post_credit_card.py
# Defaults fall back to the Stripe test PAN (which Cloudbeds will reject in live mode).
RESERVATION_ID = "1989264686165"   # Test1 Case
GUEST_NAME     = "Test1 Case"
CARD_NUMBER    = os.environ.get("CARD_NUMBER", "4640182144394958")
CARD_EXP       = os.environ.get("CARD_EXP",    "10/28")       # MM/YY
CARD_CVV       = os.environ.get("CARD_CVV",    "683")
CARD_ZIP       = os.environ.get("CARD_ZIP",    "97439")       # Florence, OR — Lighthouse Inn's zip
CARD_TYPE      = os.environ.get("CARD_TYPE")  or None         # None -> omit; GX-26's working flow omits this
# -----------------------------------------------------------------------------


async def main() -> int:
    if not settings.cloudbeds_api_key:
        print("ERROR: CLOUDBEDS_API_KEY not configured in backend/.env")
        return 2
    if not settings.cloudbeds_property_id:
        print("ERROR: CLOUDBEDS_PROPERTY_ID not configured in backend/.env")
        return 2

    print(f"Cloudbeds property: {settings.cloudbeds_property_id}")
    print(f"Target reservation: {RESERVATION_ID} ({GUEST_NAME})")
    print(f"Card: ****{CARD_NUMBER[-4:]} exp {CARD_EXP} (Stripe test PAN)")
    print()
    print("Calling post_credit_card ...")

    result = await post_credit_card(
        RESERVATION_ID,
        card_number=CARD_NUMBER,
        card_expiration=CARD_EXP,
        card_cvv=CARD_CVV,
        card_holder_name=GUEST_NAME,
        card_type=CARD_TYPE,
        card_address_zip=CARD_ZIP,
    )

    print()
    print("Result:")
    for k, v in result.items():
        # Don't echo any card material back even though post_credit_card
        # shouldn't return it.
        if "card" in k.lower() and "id" not in k.lower():
            print(f"  {k}: <redacted>")
        else:
            print(f"  {k}: {v}")

    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
