"""SMS opt-in / opt-out audit log.

One row per consent event. The latest non-revoked row for a phone is
the current state. We keep the full history (don't UPDATE existing rows)
because TCPA disputes hinge on "did you have proof of consent on
date X" -- a frozen log is the only defensible record.

Captured per event:
  - reservation context (so we can prove which booking the guest was acting on)
  - phone in E.164 (the canonical key)
  - the exact consent text the guest agreed to (version + body)
  - the source (`guest_portal`, `sms_reply_stop`, `front_desk_revoke`, ...)
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text

from app.db.database import Base


class SmsConsent(Base):
    __tablename__ = "sms_consent"

    id = Column(Integer, primary_key=True, autoincrement=True)
    reservation_id = Column(String, nullable=False, index=True)
    guest_id = Column(String, nullable=True, index=True)
    phone_e164 = Column(String, nullable=False, index=True)

    # "opt_in" or "opt_out". Latest row per phone_e164 wins.
    action = Column(String, nullable=False)

    # Where this event came from: "guest_portal", "sms_reply_stop", etc.
    source = Column(String, nullable=False)

    # Frozen copy of the consent text the user saw. If we ever change the
    # disclosure wording, old rows still prove what was actually agreed to.
    consent_text = Column(Text, nullable=True)
    consent_version = Column(String, nullable=True)

    # IP we recorded (best-effort; behind Cloudflare it's the X-Forwarded-For
    # leftmost). Useful but not load-bearing for TCPA.
    client_ip = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)

    recorded_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Twilio Lookup cache. Populated lazily the first time we attempt an SMS
    # send to this number, then re-used to avoid paying $0.005 per send.
    # NULL until we've actually called Lookup.
    #   twilio_lookup_at  -- when we asked
    #   twilio_line_type  -- "mobile" / "landline" / "voip" / "unknown"
    #   twilio_carrier    -- human-readable carrier name (Verizon, T-Mobile, ...)
    twilio_lookup_at = Column(DateTime, nullable=True)
    twilio_line_type = Column(String, nullable=True)
    twilio_carrier = Column(String, nullable=True)
