"""Guest-portal access tokens.

One row per SMS-delivered portal link. The token in the URL is the primary
key; lookup is O(1). One model serves all portal flows (checkout, preferences,
pay-by-link, cancellation) — the `purpose` column discriminates.

State machine:
  created → sms_sent → (guest opens URL) → (guest taps confirm) → confirmed → acked
                  ↑                              ↑                   ↑
              SMS attempt logged           one-shot confirm    DCS picked it up
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, String

from app.db.database import Base


class PortalToken(Base):
    __tablename__ = "portal_token"

    # 22-char base64url string from secrets.token_urlsafe(16). Globally unique.
    token = Column(String, primary_key=True)

    # What flow this token authorizes. v1: "checkout". Future: "preferences",
    # "pay", "cancel". Single table avoids per-flow joins for the DCS poll.
    purpose = Column(String, nullable=False)

    # Cloudbeds reservation context. reservation_id is the lookup key on DCS side.
    reservation_id = Column(String, nullable=False, index=True)
    first_name = Column(String, nullable=True)
    room_number = Column(String, nullable=False)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)

    # When SMS send succeeded (or null if Twilio is stubbed / send failed).
    sms_sent_at = Column(DateTime, nullable=True)

    # Twilio message SID, persisted so we can poll delivery status later. Null
    # when Twilio rejected the send, when test-mode stubbed it, or when this
    # row was created before this column existed.
    twilio_sid = Column(String, nullable=True)

    # When the guest tapped Confirm. Null until then.
    confirmed_at = Column(DateTime, nullable=True)

    # When DCS picked the confirmation off the queue. Null until DCS has acked.
    # Rows older than ~7 days post-acked can be pruned, but keeping them is
    # cheap and helps debug "did the guest actually use the link?"
    acked_at = Column(DateTime, nullable=True)
