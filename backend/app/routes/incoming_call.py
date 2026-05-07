"""Twilio inbound webhook — fires when a call arrives at the hotel number.

For v1 skeleton: returns TwiML that bridges the call to a placeholder.
For v2: pre-call screening (block list), routing-mode check (forward / AI / voicemail),
admin-header detection (route admin commands differently), etc.
"""
import logging

from fastapi import APIRouter
from fastapi.responses import Response

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/incoming-call")
async def incoming_call() -> Response:
    """Generate TwiML to route an inbound call.

    For now: returns a placeholder that announces the skeleton state and hangs up.
    Once Vapi is wired up, this will return TwiML that bridges to Vapi.
    """
    log.info("Inbound call received (skeleton mode — returning placeholder TwiML)")

    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">Hello. The Lighthouse Inn AI Reservation Agent is being set up. Please call back later. Goodbye.</Say>
    <Hangup/>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@router.post("/call-status")
async def call_status() -> dict:
    """Twilio call-status callback. Logs call lifecycle events for archival.

    TODO: capture call metadata, link to recordings, etc.
    """
    log.info("Call status callback received (not yet processed)")
    return {"received": True}
