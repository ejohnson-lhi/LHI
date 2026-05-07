"""Call routing state model — represents the current "mode" the hotel phone is in.

Set via admin-by-phone commands. Determines how inbound calls are handled:
- ai_handle: route to Vapi (Iris answers)
- forward: forward to a destination number (cell, etc.)
- voicemail: send to voicemail with a recorded greeting
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

CallRoutingMode = Literal["ai_handle", "forward", "voicemail"]


class CallRoutingState(BaseModel):
    """Current call-routing state for the hotel."""
    mode: CallRoutingMode = "ai_handle"
    destination: str | None = None  # phone number or SIP URI for forward mode
    expires_at: datetime | None = None
    fallback_on_no_answer: CallRoutingMode = "ai_handle"
    set_by: str | None = None  # who/what set this state (audit trail)
    set_at: datetime | None = None
