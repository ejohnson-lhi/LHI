"""Vapi tool endpoints — Iris calls these mid-conversation.

Vapi sends `{"message": {"toolCallList": [...], "call": {...}}}` to a tool's
configured webhook URL and expects `{"results": [{"toolCallId", "name", "result": "<json>"}]}`
back. The `_dispatch` helper handles that boilerplate so each tool stays a
simple async handler taking (args, call) -> dict.
"""
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter

from app.models.vapi_payloads import (
    VapiCallObject,
    VapiToolCallRequest,
    VapiToolCallResponse,
    VapiToolCallResult,
)
from app.tools import cloudbeds, twilio_sms

log = logging.getLogger(__name__)
router = APIRouter()


HandlerType = Callable[[dict[str, Any], VapiCallObject], Awaitable[dict]]


async def _dispatch(request: VapiToolCallRequest, handler: HandlerType) -> VapiToolCallResponse:
    """Run handler over every tool call in the request, batch results back."""
    results: list[VapiToolCallResult] = []
    for tc in request.message.toolCallList:
        name = tc.function.name
        args = tc.function.arguments or {}
        log.info("vapi tool call: name=%s id=%s args=%s", name, tc.id, args)
        try:
            result = await handler(args, request.message.call)
        except Exception:
            log.exception("Tool handler %s raised", name)
            result = {"error": "Internal error processing tool call."}
        results.append(VapiToolCallResult(
            toolCallId=tc.id, name=name, result=json.dumps(result),
        ))
    return VapiToolCallResponse(results=results)


# ---------------------------------------------------------------------------
# Handlers (one per tool, args-in dict-out)


async def _handle_check_availability(args: dict, call: VapiCallObject) -> dict:
    check_in = args.get("check_in")
    check_out = args.get("check_out")
    if not check_in or not check_out:
        return {"available": False, "message": "Need both check_in and check_out (ISO dates)."}
    rooms = await cloudbeds.check_availability(
        check_in=check_in,
        check_out=check_out,
        adults=int(args.get("adults", 2)),
        children=int(args.get("children", 0)),
        rooms=int(args.get("rooms", 1)),
    )
    if rooms is None:
        return {"available": False, "message": "Could not check availability right now."}
    return {"available": len(rooms) > 0, "room_types": rooms, "count": len(rooms)}


async def _handle_lookup_reservation(args: dict, call: VapiCallObject) -> dict:
    """Try identifiers in order of specificity: OTA → phone → last name."""
    source_id = (args.get("source_reservation_id") or "").strip()
    phone = args.get("phone_number") or call.customer.number
    last_name = (args.get("last_name") or "").strip()

    tried: list[str] = []

    if source_id:
        tried.append(f"OTA confirmation '{source_id}'")
        reservation = await cloudbeds.lookup_reservation_by_source_id(source_id)
        if reservation:
            return {"found": True, "reservation": reservation}

    if phone:
        tried.append(f"phone '{phone}'")
        reservation = await cloudbeds.lookup_reservation_by_phone(phone)
        if reservation:
            return {"found": True, "reservation": reservation}

    if last_name:
        tried.append(f"last name '{last_name}'")
        reservation = await cloudbeds.lookup_reservation_by_lastname(last_name)
        if reservation:
            return {"found": True, "reservation": reservation}

    if not tried:
        return {"found": False, "message": "Need phone_number, source_reservation_id, last_name, or caller-ID."}
    return {"found": False, "message": f"No reservation found by {' or '.join(tried)}."}


async def _handle_create_reservation(args: dict, call: VapiCallObject) -> dict:
    required = ["first_name", "last_name", "email", "check_in", "check_out", "room_type_id"]
    missing = [k for k in required if not args.get(k)]
    if missing:
        return {"success": False, "message": f"Missing required fields: {', '.join(missing)}"}
    result = await cloudbeds.create_reservation(
        first_name=args["first_name"],
        last_name=args["last_name"],
        email=args["email"],
        check_in=args["check_in"],
        check_out=args["check_out"],
        room_type_id=args["room_type_id"],
        adults=int(args.get("adults", 2)),
        children=int(args.get("children", 0)),
        phone=args.get("phone") or call.customer.number,
        estimated_arrival_time=args.get("estimated_arrival_time"),
        zip_code=args.get("zip_code", ""),
    )
    if result.get("success"):
        return {
            "success": True,
            "reservation_id": result["reservation_id"],
            "status": result.get("status"),
            "grand_total": result.get("grand_total"),
            "guest_id": result.get("guest_id"),
        }
    return {"success": False, "message": result.get("error", "Could not create reservation.")}


async def _handle_add_reservation_note(args: dict, call: VapiCallObject) -> dict:
    reservation_id = args.get("reservation_id")
    note = args.get("note")
    if not reservation_id or not note:
        return {"success": False, "message": "Need both reservation_id and note."}
    result = await cloudbeds.add_reservation_note(reservation_id, note)
    if result.get("success"):
        return {"success": True, "message": "Note added."}
    return {"success": False, "message": result.get("error", "Could not add note.")}


async def _handle_cancel_reservation(args: dict, call: VapiCallObject) -> dict:
    reservation_id = (args.get("reservation_id") or "").strip()
    if not reservation_id:
        return {"success": False, "message": "Need reservation_id to cancel."}
    reason = (args.get("reason") or "").strip() or None
    result = await cloudbeds.cancel_reservation(reservation_id, reason)
    if result.get("success"):
        return {"success": True, "reservation_id": reservation_id, "message": "Reservation canceled."}
    return {"success": False, "message": result.get("error", "Could not cancel reservation.")}


async def _handle_modify_reservation(args: dict, call: VapiCallObject) -> dict:
    reservation_id = (args.get("reservation_id") or "").strip()
    if not reservation_id:
        return {"success": False, "message": "Need reservation_id to modify."}
    new_check_out = (args.get("new_check_out") or "").strip() or None
    eta = (args.get("estimated_arrival_time") or "").strip() or None
    if not new_check_out and not eta:
        return {"success": False, "message": "Nothing to modify — provide new_check_out or estimated_arrival_time."}
    result = await cloudbeds.modify_reservation(
        reservation_id,
        new_check_out=new_check_out,
        estimated_arrival_time=eta,
    )
    if result.get("success"):
        return {
            "success": True,
            "reservation_id": reservation_id,
            "new_check_out": new_check_out,
            "estimated_arrival_time": eta,
            "message": "Reservation updated.",
        }
    return {"success": False, "message": result.get("error", "Could not modify reservation.")}


async def _handle_send_door_code(args: dict, call: VapiCallObject) -> dict:
    """Send the guest their room name + door code via SMS.

    Iris's prompt is responsible for caller-ID + room-number two-factor auth
    BEFORE this is called (see [Lockout self-service] in the prompt). This
    handler just executes — it doesn't re-verify auth.
    """
    reservation_id = (args.get("reservation_id") or "").strip()
    if not reservation_id:
        return {"sent": False, "message": "Need reservation_id."}
    phone = (args.get("phone_number") or "").strip() or call.customer.number
    if not phone:
        return {"sent": False, "message": "Need phone_number or caller-ID."}

    reservation = await cloudbeds.get_reservation_by_id(reservation_id)
    if not reservation:
        return {"sent": False, "message": f"Could not find reservation {reservation_id}."}
    room_name = reservation.get("room_name")
    door_code = reservation.get("door_code")
    if not room_name or not door_code:
        return {
            "sent": False,
            "message": "Reservation has no room and/or door code on file (room may not be assigned, or it may be a physical-key room).",
        }

    result = await twilio_sms.send_door_code_sms(phone, room_name, door_code)
    if result.get("success"):
        return {"sent": True, "sid": result.get("sid"), "message": f"Door code SMS sent to {phone}."}
    return {"sent": False, "message": result.get("error", "Could not send SMS.")}


async def _handle_send_payment_link(args: dict, call: VapiCallObject) -> dict:
    return {"sent": False, "message": "Stub — payment link integration not yet wired."}


async def _handle_send_card_link_via_sms(args: dict, call: VapiCallObject) -> dict:
    """Get-or-mint the long-lived guest-portal URL for the reservation and
    SMS it to the guest, with `?open=card` so the credit-card accordion is
    auto-expanded on arrival. Iris uses this mid-call when a reservation
    needs a card on file (e.g. incidentals deposit, no-card-booked-via-OTA
    situations).

    Args:
      reservation_id (required): public Cloudbeds reservation ID
      phone_number (optional): override the destination phone. Defaults
        to the reservation's guest_cell_phone, then guest_phone, then
        the caller's caller-ID. Iris's prompt is responsible for
        guest-identity verification BEFORE calling this — the handler
        doesn't re-verify.

    The URL is the same long-lived /g/{token} guest portal that DCS sends
    pre-arrival; if the guest already has one, we reuse it instead of
    minting a fresh token. Token lifetime is 60 days (or through 1 day
    past checkout once stay dates are known).

    SMS deliverability note: A2P 10DLC campaign approval may still be
    pending. If Twilio rejects the send, we surface the portal URL in
    the response so Iris can read it back to the guest verbally as a
    fallback. The link itself is still valid."""
    from app.config import settings as _settings
    from app.db.database import AsyncSessionLocal
    from app.routes.portal import issue_guest_token_row

    reservation_id = (args.get("reservation_id") or "").strip()
    if not reservation_id:
        return {"sent": False, "message": "Need reservation_id."}

    reservation = await cloudbeds.get_reservation_by_id(reservation_id)
    if not reservation:
        return {"sent": False, "message": f"Could not find reservation {reservation_id}."}

    # Resolve the destination phone. Iris can override via args; otherwise
    # the reservation's stored phone wins; last-ditch fallback is caller-ID.
    phone = (args.get("phone_number") or "").strip()
    if not phone:
        phone = (reservation.get("guest_cell_phone") or reservation.get("guest_phone") or "").strip()
    if not phone:
        phone = (call.customer.number or "").strip() if call.customer else ""
    if not phone:
        return {"sent": False, "message": "No phone number found for this reservation or caller."}

    first_name = (reservation.get("guest_first_name") or "").strip()

    # Get-or-create the guest portal token in-process (no HTTP round-trip
    # to ourselves). Idempotent — reuses any existing unexpired token.
    async with AsyncSessionLocal() as session:
        try:
            token, portal_url, is_new = await issue_guest_token_row(
                session, reservation_id=reservation_id,
            )
        except Exception:
            log.exception("Failed to issue guest_portal token for res %s", reservation_id)
            return {"sent": False, "message": "Could not generate the portal link."}

    # Auto-open the card accordion on arrival so the guest doesn't have to
    # hunt through the page for the right section.
    sms_portal_url = f"{portal_url}?open=card#card-section"

    # portal_test_mode redirect: during pre-A2P-approval testing, every SMS
    # gets rerouted to ERIC_CELL_NUMBER regardless of the actual guest.
    # Belt-and-suspenders so we can't accidentally text a real guest while
    # the A2P campaign is still under review.
    intended_phone = phone
    if _settings.portal_test_mode and _settings.eric_cell_number:
        log.warning(
            "send_card_link_via_sms: portal_test_mode ON — redirecting SMS for "
            "reservation %s from %s to ERIC_CELL_NUMBER %s",
            reservation_id, intended_phone, _settings.eric_cell_number,
        )
        phone = _settings.eric_cell_number

    sms_result = await twilio_sms.send_card_link_sms(
        phone_number=phone,
        portal_url=sms_portal_url,
        first_name=first_name or None,
    )

    if sms_result.get("success"):
        log.info(
            "send_card_link_via_sms: res=%s phone=%s token_new=%s sid=%s",
            reservation_id, phone, is_new, sms_result.get("sid"),
        )
        return {
            "sent": True,
            "sid": sms_result.get("sid"),
            "message": f"Card-on-file link sent to {phone}. The link stays valid through their stay.",
            # Include the URL so Iris can also read it back verbally if the
            # guest doesn't get the SMS in real time (helpful while A2P
            # campaign approval is pending and deliverability is uneven).
            "portal_url": sms_portal_url,
        }

    # SMS send failed — could be A2P-pending, an invalid number, or
    # Twilio outage. Surface the URL so the call can still succeed by
    # voice readback if Iris's prompt is set up to do that.
    log.warning(
        "send_card_link_via_sms: SMS send failed for res=%s phone=%s: %s",
        reservation_id, phone, sms_result.get("error"),
    )
    return {
        "sent": False,
        "message": (
            f"Could not send SMS ({sms_result.get('error', 'unknown error')}). "
            f"The portal link itself is valid; you can read it to "
            f"the guest: {sms_portal_url}"
        ),
        "portal_url": sms_portal_url,
    }


async def _handle_save_card_via_token(args: dict, call: VapiCallObject) -> dict:
    """Attach a Stripe-tokenized card to a Cloudbeds reservation.

    Iris's `capture_card_dtmf` collects PAN/EXP/CVC via the caller's phone
    keypad and tokenizes the card on the agent side using Cloudbeds'
    platform publishable key (sk_test/sk_live key for the connected
    account never touches our infra). Raw card material never reaches
    this backend — we receive only the resulting `token_id` (`tok_xxx`)
    plus the Stripe-side `token_card` metadata dict. We resolve the
    public reservation ID to the dashboard's internal booking_id and
    hand the token to `dashboard_save_credit_card`.

    Args expected: reservation_id, token_id, token_card (dict).

    Returns: {"success": bool, "card_id": str, "last4": str, "brand": str,
    "error": str}. `last4`/`brand` are intentionally surfaced so Iris can
    confirm verbally ("Got it — Visa ending 4958"). PAN/CVC never appear
    in the return value or the agent-side logs.
    """
    reservation_id = (args.get("reservation_id") or "").strip()
    token_id = (args.get("token_id") or "").strip()
    token_card = args.get("token_card")

    if not reservation_id:
        return {"success": False, "error": "reservation_id is required."}
    if not token_id:
        return {"success": False, "error": "token_id is required."}
    if not isinstance(token_card, dict):
        return {"success": False, "error": "token_card must be an object."}

    # Lazy imports — the cloudbeds_dashboard module pulls in playwright and
    # heavy deps when its session helpers initialize. Keep the import deferred
    # until a card-save actually fires.
    from app.tools.cloudbeds_dashboard import (
        dashboard_save_credit_card,
        get_booking_id,
    )

    booking_id = await get_booking_id(reservation_id)
    if not booking_id:
        log.warning("save_card_via_token: no booking_id for reservation=%s", reservation_id)
        return {
            "success": False,
            "error": "Could not resolve reservation to internal booking ID. "
                     "The session cookie may have expired — re-run the Playwright login.",
        }

    result = await dashboard_save_credit_card(
        booking_id=str(booking_id),
        legacy_token_id=token_id,
        token_card=token_card,
    )

    # Sanitize the response. Surface enough for Iris to confirm verbally
    # but never echo back card material that Cloudbeds might have included
    # in card_details (some dashboards include a masked PAN or other fields).
    last4 = (result.get("card_details") or {}).get("card_number") or token_card.get("last4")
    brand = (result.get("card_details") or {}).get("card_type") or token_card.get("brand")
    return {
        "success": bool(result.get("success")),
        "card_id": str(result.get("card_id") or ""),
        "last4": str(last4 or ""),
        "brand": str(brand or ""),
        "error": result.get("error") or "",
    }


async def _handle_set_call_routing(args: dict, call: VapiCallObject) -> dict:
    return {"success": False, "message": "Stub — routing state DB not yet wired."}


# ---------------------------------------------------------------------------
# Routes (one per tool, all dispatch through _dispatch)


@router.post("/check_availability")
async def check_availability(request: VapiToolCallRequest) -> VapiToolCallResponse:
    return await _dispatch(request, _handle_check_availability)


@router.post("/lookup_reservation")
async def lookup_reservation(request: VapiToolCallRequest) -> VapiToolCallResponse:
    return await _dispatch(request, _handle_lookup_reservation)


@router.post("/create_reservation")
async def create_reservation(request: VapiToolCallRequest) -> VapiToolCallResponse:
    return await _dispatch(request, _handle_create_reservation)


@router.post("/add_reservation_note")
async def add_reservation_note(request: VapiToolCallRequest) -> VapiToolCallResponse:
    return await _dispatch(request, _handle_add_reservation_note)


@router.post("/cancel_reservation")
async def cancel_reservation(request: VapiToolCallRequest) -> VapiToolCallResponse:
    return await _dispatch(request, _handle_cancel_reservation)


@router.post("/modify_reservation")
async def modify_reservation(request: VapiToolCallRequest) -> VapiToolCallResponse:
    return await _dispatch(request, _handle_modify_reservation)


@router.post("/send_door_code")
async def send_door_code(request: VapiToolCallRequest) -> VapiToolCallResponse:
    return await _dispatch(request, _handle_send_door_code)


@router.post("/send_payment_link")
async def send_payment_link(request: VapiToolCallRequest) -> VapiToolCallResponse:
    return await _dispatch(request, _handle_send_payment_link)


@router.post("/send_card_link_via_sms")
async def send_card_link_via_sms(request: VapiToolCallRequest) -> VapiToolCallResponse:
    return await _dispatch(request, _handle_send_card_link_via_sms)


@router.post("/save_card_via_token")
async def save_card_via_token(request: VapiToolCallRequest) -> VapiToolCallResponse:
    return await _dispatch(request, _handle_save_card_via_token)


@router.post("/set_call_routing")
async def set_call_routing(request: VapiToolCallRequest) -> VapiToolCallResponse:
    return await _dispatch(request, _handle_set_call_routing)
