"""Cached Pay-by-Link URLs generated via the Cloudbeds dashboard.

One row per reservation -- the latest non-expired row is the active link.
We cache so a guest revisiting the portal in the same session doesn't
re-trigger a Playwright job. Expiry is tracked separately so we can
re-generate when the link has expired (Cloudbeds typically sets these
to 7 days).

The `generation_method` column is forward-looking: if Cloudbeds ever
opens the API path to us, we can switch from 'ui_automation' to 'api'
without a schema change. The `error_message` column records the last
failure so the front desk can see context when a guest reports
trouble.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text

from app.db.database import Base


class PayByLink(Base):
    __tablename__ = "pay_by_link"

    id = Column(Integer, primary_key=True, autoincrement=True)
    reservation_id = Column(String, nullable=False, index=True)

    # The URL guests visit to enter card / pay. Null on failure rows
    # (we still write a row with error_message so we can audit attempts).
    url = Column(Text, nullable=True)

    # When Cloudbeds will reject the link (default 7d from creation in
    # their dashboard). Null when we couldn't determine it from the UI.
    expires_at = Column(DateTime, nullable=True)

    generated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    generation_method = Column(String, nullable=False, default="ui_automation")

    # On success: empty string. On failure: short description of why so
    # we can correlate with logs / SMS alerts / staff conversations.
    error_message = Column(Text, nullable=True)

    # Audit
    client_ip = Column(String, nullable=True)
