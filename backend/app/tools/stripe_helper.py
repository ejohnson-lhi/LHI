"""Stripe integration helpers.

Currently used for the guest-portal add-card flow:
  1. Browser loads Stripe.js + Elements (uses publishable key)
  2. We create a SetupIntent server-side (uses secret key)
  3. Browser confirms the SetupIntent with the entered card details
     (raw card data flows browser -> Stripe directly; never our backend)
  4. We receive the resulting PaymentMethod ID
  5. We send it to Cloudbeds via post_credit_card -- they take over as
     the card-of-file authority for that reservation

Why SetupIntent and not PaymentIntent: we want to verify the card without
moving any money. SetupIntent's whole purpose is "save a payment method
for future off-session use." Issuer-side AVS / 3DS happens during confirm.
"""
import logging
from typing import Any

import stripe

from app.config import settings

log = logging.getLogger(__name__)


def _client() -> stripe.StripeClient | None:
    """Return a Stripe client configured with our secret key, or None when
    Stripe isn't configured. Callers should fall back gracefully."""
    if not settings.stripe_api_key:
        log.warning("Stripe API key not configured")
        return None
    # The 'stripe' module is global-stateful: setting api_key once is fine
    # for our single-tenant setup. Use a per-call assignment to be explicit.
    stripe.api_key = settings.stripe_api_key
    return stripe  # type: ignore[return-value]


async def create_setup_intent(
    reservation_id: str,
    *,
    customer_email: str | None = None,
    customer_name: str | None = None,
) -> dict[str, Any]:
    """Create a Stripe SetupIntent for the off-session card-save flow.

    Returns {"success": True, "client_secret": "...", "intent_id": "..."}
    or {"success": False, "error": "..."}. Never raises -- caller branches
    on the success flag.

    Metadata fields attach our reservation ID to the intent so the Stripe
    dashboard ties every setup attempt back to a booking (helps the front
    desk during disputes).
    """
    s = _client()
    if s is None:
        return {"success": False, "error": "Stripe is not configured."}
    try:
        intent = s.SetupIntent.create(
            usage="off_session",
            payment_method_types=["card"],
            metadata={
                "reservation_id": reservation_id,
                "source": "guest_portal",
                **({"guest_email": customer_email} if customer_email else {}),
                **({"guest_name": customer_name} if customer_name else {}),
            },
        )
    except stripe.error.StripeError as e:  # type: ignore[attr-defined]
        log.warning("Stripe SetupIntent create failed for res=%s: %s", reservation_id, e)
        return {"success": False, "error": str(e)}
    log.info("Stripe SetupIntent created for res=%s id=%s", reservation_id, intent.id)
    return {
        "success": True,
        "client_secret": intent.client_secret,
        "intent_id": intent.id,
    }


async def retrieve_payment_method(payment_method_id: str) -> dict[str, Any]:
    """Look up a PaymentMethod by ID -- mainly to extract last-4 / brand for
    confirmation logging. Returns {"success": True, "card": {...}} or error."""
    s = _client()
    if s is None:
        return {"success": False, "error": "Stripe is not configured."}
    try:
        pm = s.PaymentMethod.retrieve(payment_method_id)
    except stripe.error.StripeError as e:  # type: ignore[attr-defined]
        log.warning("Stripe PaymentMethod retrieve failed for %s: %s", payment_method_id, e)
        return {"success": False, "error": str(e)}
    card = getattr(pm, "card", None)
    if card is None:
        return {"success": False, "error": "PaymentMethod has no card detail (wrong type?)."}
    return {
        "success": True,
        "card": {
            "brand": card.brand,
            "last4": card.last4,
            "exp_month": card.exp_month,
            "exp_year": card.exp_year,
        },
    }
