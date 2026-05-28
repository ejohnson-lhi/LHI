"""Per-reservation room-preference list submitted by the guest via the
portal's drag-and-drop preference selector.

Storage shape:
  reservation_id  (PK, one row per reservation)
  prioritized_json  JSON-encoded ordered list of preference keys, e.g.
                    ["upstairs", "bathtub", "quiet_side"]. Items appear
                    in the guest's stated priority order — earlier items
                    matter more.
  updated_at        Last save timestamp.

What "in the list" means: the guest has explicitly placed this preference
in the "matters to me" zone. Anything NOT in the list is implicitly "no
preference" — front-desk treats it as a don't-care.

Lifecycle:
  - Created/updated by /g/{token}/preferences and /h{stem}/preferences
    POST handlers, pre-stay only (locked once phase becomes in_house).
  - Read by /portal/guest-preferences/{reservation_id} for DCS staff
    views during morning-of room assignment.

Why not normalize into a (reservation_id, preference_key, priority) table?
The data is read as a whole and rarely partially-updated. A JSON column
keeps reads/writes one row per reservation and lets the canonical
preference list evolve without schema changes. The trade-off is no
SQL-side filtering by preference, which we don't need — staff views
fetch a single guest's prefs at a time.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, String, Text

from app.db.database import Base


class GuestPreference(Base):
    __tablename__ = "guest_preference"

    reservation_id = Column(String, primary_key=True)
    prioritized_json = Column(Text, nullable=False, default="[]")
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    client_ip = Column(String, nullable=True)
    user_agent = Column(String(500), nullable=True)
