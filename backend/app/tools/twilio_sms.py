"""Twilio SMS sending wrapper.

Auth: HTTP Basic (AccountSid:AuthToken) against
https://api.twilio.com/2010-04-01/Accounts/{Sid}/Messages.json
"""
import logging

import httpx

from app.config import settings

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0


async def send_sms(to: str, body: str, *, from_: str | None = None) -> dict:
    """Send an SMS via Twilio's Messages API.

    `from_` defaults to TWILIO_HOTEL_NUMBER. The sender must be a Twilio-owned
    SMS-capable number on the same account.

    Returns {"success": True, "sid": "<message_sid>"} or
    {"success": False, "error": "..."}. Never raises.
    """
    if not settings.twilio_account_sid or not settings.twilio_auth_token:
        return {"success": False, "error": "Twilio credentials not configured."}

    url = f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Messages.json"
    auth = (settings.twilio_account_sid, settings.twilio_auth_token)
    data: dict[str, str] = {"To": to, "Body": body}

    # Prefer Messaging Service (required for A2P 10DLC-registered traffic);
    # fall back to plain From=number for dev / unregistered testing. Explicit
    # `from_` kwarg always wins (useful for one-off overrides in tests).
    if from_:
        data["From"] = from_
    elif settings.twilio_messaging_service_sid:
        data["MessagingServiceSid"] = settings.twilio_messaging_service_sid
    elif settings.twilio_hotel_number:
        data["From"] = settings.twilio_hotel_number
    else:
        return {"success": False, "error": "No Twilio sender configured (need MessagingServiceSid or From number)."}

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.post(url, auth=auth, data=data)
    except httpx.TimeoutException:
        log.warning("Twilio SMS timed out")
        return {"success": False, "error": "Twilio SMS timed out."}
    except httpx.HTTPError as e:
        log.warning("Twilio SMS HTTP error: %s", e)
        return {"success": False, "error": str(e)}

    if response.status_code not in (200, 201):
        log.warning("Twilio SMS HTTP %s: %s", response.status_code, response.text[:300])
        return {"success": False, "error": f"HTTP {response.status_code}: {response.text[:200]}"}

    body_json = response.json()
    sid = body_json.get("sid")
    log.info("Twilio SMS sent: sid=%s to=%s", sid, to)
    return {"success": True, "sid": sid, "status": body_json.get("status")}


async def send_sms_to_eric(message: str) -> dict:
    """Send a routine notification SMS to Eric's cell.

    Used for: room checked out notifications, late checkout requests,
    extended stay requests, etc. (See [Check-Out Requests] in the prompt.)
    """
    if not settings.eric_cell_number:
        return {"success": False, "error": "ERIC_CELL_NUMBER not configured."}
    return await send_sms(settings.eric_cell_number, message)


async def send_door_code_sms(phone_number: str, room_name: str, door_code: str) -> dict:
    """Send the room name and door code to a verified guest's phone."""
    body = (
        f"Lighthouse Inn check-in info:\n"
        f"Room: {room_name}\n"
        f"Door code: {door_code}\n"
        f"If the code doesn't work, give it 2 minutes for the lock to reset and try again.\n"
        f"Reply STOP to opt out."
    )
    return await send_sms(phone_number, body)
