"""Stripe payment integration.

For card capture, we use Twilio Pay Connector configured for Stripe — the
caller types card digits via DTMF, Twilio sends the data to Stripe (PCI-safe),
and Stripe returns a Payment Method. We then create a Stripe Customer with
that payment method and pass the Customer ID to Cloudbeds as the cardToken.

For v1: stub functions. Wire up actual Stripe API in the next iteration.
"""
import logging

from app.config import settings

log = logging.getLogger(__name__)


async def create_customer_with_payment_method(
    payment_method_id: str,
    name: str | None = None,
    email: str | None = None,
) -> str | None:
    """Create a Stripe Customer and attach a payment method.

    Returns the Stripe Customer ID (string starting with 'cus_'), which gets
    passed to Cloudbeds as the cardToken parameter on postReservation.

    TODO: wire up actual Stripe API call.
    """
    log.info(f"[STUB] create_customer_with_payment_method({payment_method_id}, name={name})")
    return None


async def charge_customer(
    customer_id: str,
    amount_cents: int,
    description: str | None = None,
) -> str | None:
    """Charge a Stripe Customer immediately (used for same-day reservations).

    Returns the Stripe Charge ID (string starting with 'ch_'), which gets
    passed to Cloudbeds as the paymentAuthorizationCode parameter.

    TODO: wire up actual Stripe API call.
    """
    log.info(f"[STUB] charge_customer({customer_id}, amount={amount_cents})")
    return None
