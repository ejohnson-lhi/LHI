"""Per-reservation log of guest questions asked via the portal's FAQ
section. Captures both "I tapped an FAQ entry" and "I asked Iris (LLM)"
events. Drives:

  1. Staff review -- which questions weren't well-served by the existing
     FAQ list, so we can curate new entries.
  2. Usage reporting -- time-of-day distribution, day-of-stay, sequence
     of questions per guest (their exploration arc).
  3. Rate limiting -- count of llm_used=true rows per reservation per
     UTC day drives the throttle delay + daily cap.

Stored on the iris droplet alongside the other portal tables. Pulled
from DCS via a shared-secret read endpoint (defined in routes/portal.py)
for the staff review UI.
"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from app.db.database import Base


class GuestQa(Base):
    __tablename__ = "guest_qa"

    id = Column(Integer, primary_key=True, autoincrement=True)
    reservation_id = Column(String, index=True, nullable=False)
    asked_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    # Raw text the guest typed. Capped at 1000 chars on the way in --
    # serves as the search key when staff cluster similar unanswered
    # questions.
    question_text = Column(Text, nullable=False)
    # JSON array of FAQ slugs that the matcher returned for this query,
    # in score order. Empty list = no FAQ hits (typically the case when
    # llm_used=True). Single-element list with a leading "selected:"
    # marker indicates the guest TAPPED that specific entry (we may add
    # this later if click-through telemetry becomes useful).
    matched_faq_slugs_json = Column(Text, nullable=False, default="[]")
    # True iff we made an Anthropic call. False = FAQ-only interaction.
    llm_used = Column(Boolean, nullable=False, default=False)
    llm_response_text = Column(Text, nullable=True)
    llm_input_tokens = Column(Integer, nullable=True)
    llm_output_tokens = Column(Integer, nullable=True)
    # Computed from reservation.check_in at write time. Negative for
    # pre-stay queries (-2 = two days before arrival), 0 = arrival day,
    # 1.. = nights into stay. Null when we couldn't load the reservation.
    day_of_stay = Column(Integer, nullable=True)
    client_ip = Column(String, nullable=True)
    user_agent = Column(String(500), nullable=True)
    # Staff-review state. NULL = not yet reviewed. Once a staff member
    # has read this row, set to the review timestamp. Promoting to a
    # permanent FAQ entry happens out-of-band (edit the KB markdown)
    # but we set `promoted_to_kb` so the review UI can hide it.
    reviewed_at = Column(DateTime, nullable=True)
    promoted_to_kb = Column(Boolean, nullable=False, default=False)
