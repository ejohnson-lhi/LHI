"""Step 1 of the paymentMethodId-path follow-up test.

Creates a Stripe SetupIntent on the configured Stripe account. The
resulting client_secret is what the browser-side capture page
(card_capture.html) needs in order to confirm a card and produce a
PaymentMethod ID we can hand to Cloudbeds' postCreditCard.

IMPORTANT — Stripe account identity matters:
  Cloudbeds Payments on this property is wired to Stripe Connect
  account acct_1RYA4xEJ572tmEoR. For Cloudbeds to be able to retrieve
  the resulting PaymentMethod via postCreditCard(paymentMethodId=...),
  the SetupIntent MUST be created on the same Stripe account. Either:
    (a) STRIPE_API_KEY in backend/.env is for that exact account, OR
    (b) override via env var:
          STRIPE_API_KEY=sk_live_... STRIPE_PUBLISHABLE_KEY=pk_live_... \
            python scripts/create_setup_intent.py
  If you have a Stripe Express login for the property, the keys live
  under Developers -> API keys.

Run from backend dir:
    .venv\\Scripts\\python scripts\\create_setup_intent.py

Output: client_secret + publishable_key + the URL to open card_capture.html
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import quote

backend_root = Path(__file__).parent.parent
os.chdir(backend_root)
sys.path.insert(0, str(backend_root))

from app.config import settings  # noqa: E402
from app.tools import stripe_helper  # noqa: E402

RESERVATION_ID = "1989264686165"   # Test1 Case
HTML_PAGE      = Path(__file__).parent / "card_capture.html"


async def main() -> int:
    # Honor STRIPE_API_KEY env override (so the user can point at the
    # connected account without editing .env).
    override_secret = os.environ.get("STRIPE_API_KEY")
    if override_secret:
        import stripe
        stripe.api_key = override_secret
        # Bypass the helper's settings.stripe_api_key read by patching:
        original_client = stripe_helper._client
        stripe_helper._client = lambda: stripe  # type: ignore[assignment]
        active_key_source = "STRIPE_API_KEY env var"
    else:
        active_key_source = "backend/.env STRIPE_API_KEY"

    publishable = (
        os.environ.get("STRIPE_PUBLISHABLE_KEY")
        or getattr(settings, "stripe_publishable_key", None)
        or ""
    )
    if not publishable:
        print(
            "WARNING: no Stripe PUBLISHABLE key configured. The browser page "
            "needs one to load Elements. Set STRIPE_PUBLISHABLE_KEY env var or "
            "add stripe_publishable_key to app/config.py + backend/.env."
        )
        print()

    print(f"Using Stripe secret key from: {active_key_source}")
    print(f"Creating SetupIntent for reservation {RESERVATION_ID} ...")
    print()

    result = await stripe_helper.create_setup_intent(
        RESERVATION_ID,
        customer_email="test1@laserbite.com",
        customer_name="Test1 Case",
    )

    if not result.get("success"):
        print(f"FAILED: {result.get('error')}")
        return 1

    client_secret = result["client_secret"]
    intent_id = result["intent_id"]

    print("SetupIntent created.")
    print(f"  intent_id:     {intent_id}")
    print(f"  client_secret: {client_secret}")
    print()
    print("Next step — capture a card in the browser:")
    print()
    print(f"  1. Open: file:///{HTML_PAGE.resolve().as_posix()}")
    print(
        "     (Stripe Elements requires file:// or https:// origin; on Windows "
        "the file:// path works for testing.)"
    )
    print("  2. Paste these into the form:")
    print(f"       Publishable key: {publishable or '<set STRIPE_PUBLISHABLE_KEY>'}")
    print(f"       Client secret:   {client_secret}")
    print("  3. Enter a real card and submit.")
    print("  4. Copy the resulting PaymentMethod ID (pm_...) from the page.")
    print("  5. Hand it to Cloudbeds:")
    print(f"       PM_ID=<pm_...> .venv/Scripts/python.exe scripts/test_post_credit_card_pmid.py")
    print()
    # Convenience: also dump a query-string version of the URL so the user
    # can click through without copy-pasting (publishable key has no PII).
    if publishable:
        qs = f"?pk={quote(publishable)}&cs={quote(client_secret)}"
        print("  Shortcut (publishable key + client_secret prefilled in URL):")
        print(f"    file:///{HTML_PAGE.resolve().as_posix()}{qs}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
