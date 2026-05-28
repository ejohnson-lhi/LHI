"""Per-reservation flag: has the guest confirmed their address & phone via
the portal at least once?

Why this matters: when a reservation comes in from an OTA (Booking.com,
Expedia, etc.), Cloudbeds pre-populates the guest's address with whatever
the OTA had on file — which is often stale or wrong. Even when our portal
shows "address looks complete" based on Cloudbeds' data, we want the
guest to actively review it once before treating the contact section as
"done." This row gets written the first time the guest taps Save on the
contact form (no matter whether they actually changed anything).

Keyed by reservation_id only (not token) because:
  - The portal has two front-doors (/g/{token} and /h{stem}); the latter
    doesn't have a token row to attach this to.
  - Multiple PortalToken rows can exist per reservation over time; the
    acknowledgement is a property of the reservation, not the token.

One row per reservation (PK = reservation_id). On re-save we UPDATE the
timestamp rather than INSERT a new row -- audit history of contact edits
is tracked separately via SmsConsent rows and Cloudbeds put_guest_contact
audit, not here. This row is purely a "first acknowledgement" gate.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, String

from app.db.database import Base


class ContactAcknowledgement(Base):
    __tablename__ = "contact_acknowledgement"

    reservation_id = Column(String, primary_key=True)
    acknowledged_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    client_ip = Column(String, nullable=True)
    user_agent = Column(String(500), nullable=True)
