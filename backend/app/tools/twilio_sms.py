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

    Test-mode safety: when settings.sms_test_redirect is on (the default until
    A2P 10DLC is approved), the message is redirected to ERIC_CELL_NUMBER and
    the real recipient is never texted. send_sms is the single chokepoint for
    all outbound SMS, so guarding here covers every call site. Fail-closed: if
    the redirect is on but ERIC_CELL_NUMBER is empty, we refuse to send.

    Returns {"success": True, "sid": "<message_sid>"} or
    {"success": False, "error": "..."}. Never raises. A redirected message
    also carries {"redirected_to_eric": True, "original_to": "<real number>"}.
    """
    # Global test-mode redirect (see docstring). Runs before anything else so
    # no code path can bypass it.
    original_to = to
    redirected = False
    if settings.sms_test_redirect:
        orig_last4 = original_to[-4:] if original_to and len(original_to) >= 4 else "?"
        if not settings.eric_cell_number:
            log.warning(
                "send_sms: SMS_TEST_REDIRECT on but ERIC_CELL_NUMBER empty; "
                "refusing to send (intended recipient ends %s)", orig_last4)
            return {"success": False,
                    "error": "SMS_TEST_REDIRECT on but ERIC_CELL_NUMBER not configured; refusing to send."}
        if to != settings.eric_cell_number:
            log.info("send_sms: TEST redirect -> Eric (intended recipient ends %s)", orig_last4)
            # Prefix so Eric can tell which guest each redirected text was for.
            body = f"[TEST->{orig_last4}] {body}"
        to = settings.eric_cell_number
        redirected = True

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
    log.info("Twilio SMS sent: sid=%s to=%s%s", sid, to,
             " (redirected)" if redirected else "")
    result = {"success": True, "sid": sid, "status": body_json.get("status")}
    if redirected:
        result["redirected_to_eric"] = True
        result["original_to"] = original_to
    return result


async def get_message_status(message_sid: str) -> dict:
    """Look up delivery status for a previously-sent message by SID.

    Returns the relevant Twilio fields: status, error_code, error_message,
    to, from, date_sent, date_updated. status is one of:
      accepted, queued, sending, sent, receiving, received, delivered,
      undelivered, failed, read, scheduled, canceled.
    """
    if not settings.twilio_account_sid or not settings.twilio_auth_token:
        return {"success": False, "error": "Twilio credentials not configured."}

    url = (f"https://api.twilio.com/2010-04-01/Accounts/"
           f"{settings.twilio_account_sid}/Messages/{message_sid}.json")
    auth = (settings.twilio_account_sid, settings.twilio_auth_token)
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.get(url, auth=auth)
    except httpx.HTTPError as e:
        return {"success": False, "error": str(e)}

    if response.status_code == 404:
        return {"success": False, "error": "SID not found on this account."}
    if response.status_code != 200:
        return {"success": False, "error": f"HTTP {response.status_code}: {response.text[:200]}"}

    body = response.json()
    return {
        "success": True,
        "sid": body.get("sid"),
        "status": body.get("status"),
        "error_code": body.get("error_code"),
        "error_message": body.get("error_message"),
        "to": body.get("to"),
        "from": body.get("from"),
        "date_sent": body.get("date_sent"),
        "date_updated": body.get("date_updated"),
    }


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


async def send_card_link_sms(phone_number: str, portal_url: str, *, first_name: str | None = None) -> dict:
    """SMS the guest a one-shot secure link to add a card to their
    reservation. The portal page handles tokenization via Stripe.js so
    the PAN never touches our backend. Keep the body short — A2P
    deliverability favors brief, identifiable messages."""
    greeting = f"Hi {first_name}," if first_name and first_name.strip() else "Hello,"
    body = (
        f"{greeting} Lighthouse Inn: add a card on file securely "
        f"via this link: {portal_url}\n"
        f"Link expires in 24 hours. Reply STOP to opt out."
    )
    return await send_sms(phone_number, body)
