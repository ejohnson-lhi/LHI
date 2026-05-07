"""Cloudbeds API client wrapper.

Auth: `Authorization: Bearer <api_key>` against api.cloudbeds.com/api/v1.3.
Reference implementation: D:\\2-Work\\ComputerSoftwareDevelopment\\Cloudbeds-GX26\\CloudbedsAPI.bas
"""
import logging
import re
from datetime import date, timedelta
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)

CLOUDBEDS_BASE_URL = "https://api.cloudbeds.com/api/v1.3"

# Tight timeout: these calls happen mid-conversation, so latency stacks on
# top of STT + LLM + TTS round-trips.
DEFAULT_TIMEOUT_SECONDS = 5.0


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.cloudbeds_api_key}"}


def normalize_phone_e164(raw: str | None, default_country_code: str = "1") -> str | None:
    """Normalize to E.164 (+15417295563). Returns None if not parseable."""
    if not raw:
        return None
    cleaned = re.sub(r"[^\d+]", "", raw)
    if cleaned.startswith("+"):
        digits = cleaned[1:]
        return f"+{digits}" if 7 <= len(digits) <= 15 else None
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) == 10:
        return f"+{default_country_code}{digits}"
    if len(digits) == 11 and digits.startswith(default_country_code):
        return f"+{digits}"
    return None


async def _get(endpoint: str, params: dict[str, Any] | None = None) -> dict | None:
    """GET a Cloudbeds endpoint. Returns parsed JSON, or None on any failure.

    Never raises — agent calls must fall back gracefully rather than crash.
    """
    if not settings.cloudbeds_api_key:
        log.warning("Cloudbeds API key not configured; skipping %s", endpoint)
        return None
    url = f"{CLOUDBEDS_BASE_URL}/{endpoint.lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.get(url, params=params, headers=_auth_headers())
    except httpx.TimeoutException:
        log.warning("Cloudbeds %s timed out after %ss", endpoint, DEFAULT_TIMEOUT_SECONDS)
        return None
    except httpx.HTTPError as e:
        log.warning("Cloudbeds %s HTTP error: %s", endpoint, e)
        return None

    if response.status_code != 200:
        log.warning("Cloudbeds %s HTTP %s: %s", endpoint, response.status_code, response.text[:300])
        return None
    body = response.json()
    if not body.get("success", False):
        log.warning("Cloudbeds %s success=false: %s", endpoint, str(body)[:300])
        return None
    return body


def _extract_phones_from_reservation(reservation: dict) -> list[str]:
    """Pull all phone strings from a reservation record.

    Cloudbeds places phones under different keys depending on endpoint + flags:
    top-level, inside `guestList[id]`, or inside `guests[]`.
    """
    phones: list[str] = []
    for key in ("guestPhone", "guestCellPhone"):
        v = reservation.get(key)
        if v:
            phones.append(v)
    guest_list = reservation.get("guestList") or {}
    if isinstance(guest_list, dict):
        for guest in guest_list.values():
            if isinstance(guest, dict):
                for key in ("guestPhone", "guestCellPhone"):
                    v = guest.get(key)
                    if v:
                        phones.append(v)
    guests = reservation.get("guests") or []
    if isinstance(guests, list):
        for guest in guests:
            if isinstance(guest, dict):
                for key in ("guestPhone", "guestCellPhone"):
                    v = guest.get(key)
                    if v:
                        phones.append(v)
    return phones


def _format_iso_date_long(s: str | None) -> str | None:
    """ISO 'YYYY-MM-DD' -> 'Sunday, May 3, 2026'. Natural language is
    less ambiguous for the LLM (avoids midnight-UTC timezone shifts when
    rendering bare ISO dates).
    """
    if not s:
        return s
    try:
        d = date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return s
    return d.strftime("%A, %B ") + str(d.day) + d.strftime(", %Y")


def _stay_phase(start_iso: str | None, end_iso: str | None) -> str:
    """Derive a date-aware label so Iris doesn't have to compare dates herself.

    Cloudbeds doesn't auto-progress reservation `status` from "checked_in" to
    "checked_out" — that's a manual workflow step. So a stay that ended a
    week ago can still show status="checked_in", which Iris was treating as
    "currently here." This explicit label fixes that.
    """
    today = date.today()
    try:
        start = date.fromisoformat(str(start_iso)[:10]) if start_iso else None
        end = date.fromisoformat(str(end_iso)[:10]) if end_iso else None
    except (ValueError, TypeError):
        return "unknown"
    if start is None or end is None:
        return "unknown"
    if today < start:
        return "future"
    if today == start:
        return "arriving_today"
    if today > end:
        return "past"
    if today == end:
        # Checkout day — guest is still in-house through morning checkout.
        return "departing_today"
    if today + timedelta(days=1) == end:
        return "in_house_departing_tomorrow"
    return "in_house"


def _summarize_reservation(reservation: dict) -> dict:
    """Project a Cloudbeds reservation down to fields Iris needs."""
    door_code: str | None = None
    for cf in reservation.get("customFields") or []:
        if isinstance(cf, dict) and (cf.get("customFieldName") or "").lower() == "door code":
            door_code = cf.get("customFieldValue") or None
            break

    room_name: str | None = None
    assigned = reservation.get("assigned") or []
    if assigned and isinstance(assigned[0], dict):
        room_name = assigned[0].get("roomName")
    if not room_name:
        guest_list = reservation.get("guestList") or {}
        if isinstance(guest_list, dict):
            for guest in guest_list.values():
                if isinstance(guest, dict) and guest.get("isMainGuest"):
                    room_name = guest.get("roomName")
                    break

    source_id = reservation.get("sourceID")
    start_iso = reservation.get("startDate")
    end_iso = reservation.get("endDate")
    return {
        "reservation_id": reservation.get("reservationID"),
        "guest_name": reservation.get("guestName"),
        "check_in": _format_iso_date_long(start_iso),
        "check_out": _format_iso_date_long(end_iso),
        "stay_phase": _stay_phase(start_iso, end_iso),
        "status": reservation.get("status"),
        "source": reservation.get("sourceName") or reservation.get("source"),
        "source_id": source_id,
        # Cloudbeds source IDs follow a prefix convention: `s-N` = direct
        # (Website s-1, Phone s-3, etc.); `ss-XXXXXX-N` = OTA / third-party.
        # Exception: bookings Iris creates herself use a third-party-flagged
        # "AI voice agent" travel-agent source (postReservation only accepts
        # third-party sourceIDs) but are operationally direct. Treat that
        # specific configured source as direct too.
        "is_direct_booking": bool(source_id and (
            (source_id.startswith("s-") and not source_id.startswith("ss-"))
            or source_id == settings.cloudbeds_reservation_source_id
        )),
        "room_name": room_name,
        "door_code": door_code,
        "balance_due": reservation.get("balance"),
    }


PAGE_SIZE = 100
MAX_PAGES = 10  # safety: cap one lookup at 1000 reservations / 10 API calls


async def lookup_reservation_by_phone(phone_number: str) -> dict | None:
    """Look up a reservation by guest phone number.

    Cloudbeds' getReservations has no phone filter, so we pull pages of
    recent + upcoming reservations and filter client-side after E.164
    normalization. Pages are fetched sequentially with early termination
    on the first match — most callers' reservations land on page 1 since
    the date window is centered on today.
    """
    target = normalize_phone_e164(phone_number)
    if not target:
        log.info("lookup_reservation_by_phone: could not normalize '%s'", phone_number)
        return None

    today = date.today()
    base_params = {
        "propertyID": settings.cloudbeds_property_id,
        "checkInFrom": (today - timedelta(days=30)).isoformat(),
        "checkInTo": (today + timedelta(days=120)).isoformat(),
        "checkOutFrom": (today - timedelta(days=7)).isoformat(),
        "includeGuestsDetails": "true",
        "includeAllRooms": "true",
        "includeCustomFields": "true",
        # Recently-modified records first → callers who just booked or are
        # in-house land on page 1, which makes early termination win more often.
        "sortByRecent": "true",
        "pageSize": str(PAGE_SIZE),
    }

    matches: list[dict] = []
    total_scanned = 0
    for page_num in range(1, MAX_PAGES + 1):
        params = dict(base_params, pageNumber=str(page_num))
        body = await _get("getReservations", params=params)
        if not body:
            break
        data = body.get("data") or []
        if not isinstance(data, list):
            break
        page_count = len(data)
        api_total = body.get("total")
        log.info(
            "lookup_reservation_by_phone: page=%d count=%d total=%s scanned=%d",
            page_num, page_count, api_total, total_scanned + page_count,
        )
        total_scanned += page_count
        if page_count == 0:
            break

        for reservation in data:
            if not isinstance(reservation, dict):
                continue
            for raw_phone in _extract_phones_from_reservation(reservation):
                if normalize_phone_e164(raw_phone) == target:
                    matches.append(reservation)
                    break
        if matches:
            break  # early termination — common case

        if page_count < PAGE_SIZE:
            break  # partial page = last page
        if api_total is not None and total_scanned >= api_total:
            break

    if not matches:
        log.info("lookup_reservation_by_phone: no match for %s in %d reservations", target, total_scanned)
        return None

    # Prefer the reservation closest to today, with future arrivals beating
    # past ones. Most callers only have one match.
    def _sort_key(r: dict) -> tuple[bool, int]:
        start = r.get("startDate") or "9999-12-31"
        try:
            start_date = date.fromisoformat(start)
        except ValueError:
            return (True, 10_000)
        return (start_date < today, abs((start_date - today).days))
    matches.sort(key=_sort_key)
    return _summarize_reservation(matches[0])


# ---------------------------------------------------------------------------
# The remaining functions are still stubs; wire up as project progresses.

async def lookup_reservation_by_lastname(last_name: str) -> dict | None:
    """Look up a reservation by guest last name.

    Cloudbeds supports server-side `lastName` filtering on getReservations.
    May match multiple guests with the same last name; we return the one
    closest to today (future arrivals preferred). Iris can ask for first
    name to disambiguate if needed.
    """
    needle = (last_name or "").strip()
    if not needle:
        return None

    today = date.today()
    base_params = {
        "propertyID": settings.cloudbeds_property_id,
        "lastName": needle,
        "checkInFrom": (today - timedelta(days=30)).isoformat(),
        "checkInTo": (today + timedelta(days=120)).isoformat(),
        "checkOutFrom": (today - timedelta(days=7)).isoformat(),
        "includeGuestsDetails": "true",
        "includeAllRooms": "true",
        "includeCustomFields": "true",
        "sortByRecent": "true",
        "pageSize": str(PAGE_SIZE),
    }

    matches: list[dict] = []
    for page_num in range(1, MAX_PAGES + 1):
        params = dict(base_params, pageNumber=str(page_num))
        body = await _get("getReservations", params=params)
        if not body:
            break
        data = body.get("data") or []
        if not isinstance(data, list) or not data:
            break
        matches.extend(r for r in data if isinstance(r, dict))
        if len(data) < PAGE_SIZE:
            break
        api_total = body.get("total")
        if api_total is not None and len(matches) >= api_total:
            break

    if not matches:
        log.info("lookup_reservation_by_lastname: no match for %s", needle)
        return None

    def _sort_key(r: dict) -> tuple[bool, int]:
        start = r.get("startDate") or "9999-12-31"
        try:
            start_date = date.fromisoformat(start)
        except ValueError:
            return (True, 10_000)
        return (start_date < today, abs((start_date - today).days))

    matches.sort(key=_sort_key)
    return _summarize_reservation(matches[0])


async def get_reservation_by_id(reservation_id: str) -> dict | None:
    """Fetch a single reservation directly by its Cloudbeds reservation ID.

    Used when Iris has already done a `lookup_reservation_by_*` and wants to
    re-fetch (or fetch related details) using the now-known ID without
    going through the windowed search again.
    """
    needle = (reservation_id or "").strip()
    if not needle:
        return None
    body = await _get("getReservation", params={"reservationID": needle})
    if not body:
        return None
    data = body.get("data")
    if not isinstance(data, dict):
        return None
    return _summarize_reservation(data)


async def lookup_reservation_by_source_id(source_reservation_id: str) -> dict | None:
    """Look up a reservation by the OTA's confirmation number.

    Useful when callers booked via Expedia / Booking.com / similar and have
    that OTA confirmation handy but not phone or name. Cloudbeds stores it
    as `thirdPartyIdentifier` on the reservation; we filter via the
    `sourceReservationId` query param.
    """
    needle = (source_reservation_id or "").strip()
    if not needle:
        return None
    params = {
        "propertyID": settings.cloudbeds_property_id,
        "sourceReservationId": needle,
        "includeGuestsDetails": "true",
        "includeAllRooms": "true",
        "includeCustomFields": "true",
    }
    body = await _get("getReservations", params=params)
    if not body:
        return None
    data = body.get("data") or []
    if not isinstance(data, list) or not data:
        log.info("lookup_reservation_by_source_id: no match for %s", needle)
        return None
    return _summarize_reservation(data[0])


async def check_availability(
    *,
    check_in: str,
    check_out: str,
    adults: int = 2,
    children: int = 0,
    rooms: int = 1,
) -> list[dict] | None:
    """Check available room types for the given dates and party size.

    Returns a list of room-type summaries (one per distinct room type, with all
    applicable rate plans nested) or None on failure. Cloudbeds returns one
    response row per (room_type, rate_plan) combination — we group by room
    type so Iris can see the lowest rate per type without scanning duplicates.
    """
    params = {
        "propertyIDs": settings.cloudbeds_property_id,
        "startDate": check_in,
        "endDate": check_out,
        "rooms": str(rooms),
        "adults": str(adults),
        "children": str(children),
        "detailedRates": "true",
    }
    body = await _get("getAvailableRoomTypes", params=params)
    if not body:
        return None
    data = body.get("data")
    if not isinstance(data, list):
        return None
    log.info("check_availability: %d property entries for %s..%s adults=%d",
             len(data), check_in, check_out, adults)
    return _summarize_availability(data)


def _avg_nightly(detailed_rates: object) -> float | None:
    if not isinstance(detailed_rates, list) or not detailed_rates:
        return None
    rates: list[float] = []
    for d in detailed_rates:
        if not isinstance(d, dict):
            continue
        r = d.get("rate")
        if r is None:
            continue
        try:
            rates.append(float(r))
        except (TypeError, ValueError):
            continue
    if not rates:
        return None
    return round(sum(rates) / len(rates), 2)


def _summarize_availability(data: list) -> list[dict]:
    """Group Cloudbeds availability rows by room type with nested rate plans."""
    grouped: dict[str, dict] = {}
    for prop in data:
        if not isinstance(prop, dict):
            continue
        currency = (prop.get("propertyCurrency") or {}).get("currencyCode", "USD")
        for r in prop.get("propertyRooms") or []:
            if not isinstance(r, dict):
                continue
            rt_id = r.get("roomTypeID")
            if not rt_id:
                continue
            if rt_id not in grouped:
                grouped[rt_id] = {
                    "room_type_id": rt_id,
                    "room_type_name": r.get("roomTypeName"),
                    "max_guests": _maybe_int(r.get("maxGuests")),
                    "rooms_available": r.get("roomsAvailable"),
                    "currency": currency,
                    "rate_plans": [],
                }
            grouped[rt_id]["rate_plans"].append({
                "name": r.get("ratePlanNamePublic"),
                "total": r.get("roomRate"),
                "nightly": _avg_nightly(r.get("roomRateDetailed")),
            })
    # Sort each room type's rate plans cheapest first so Iris sees the best
    # quote at index 0.
    for entry in grouped.values():
        entry["rate_plans"].sort(key=lambda rp: (rp.get("total") is None, rp.get("total") or 0))
    return list(grouped.values())


def _maybe_int(v: object) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


async def create_reservation(
    *,
    first_name: str,
    last_name: str,
    email: str,
    check_in: str,
    check_out: str,
    room_type_id: str,
    adults: int = 2,
    children: int = 0,
    phone: str | None = None,
    estimated_arrival_time: str | None = None,
    country: str = "US",
    zip_code: str = "",
) -> dict:
    """Create a new reservation in Cloudbeds (v1: no payment / pet fee / special requests).

    Payment, pet fees, and special-requests notes are deliberately separate calls
    handled in their own chapters (Stripe wiring, postItem, postReservationNote).

    Returns a dict — {"success": True, "reservation_id": "..."} on success,
    or {"success": False, "error": "..."} on failure. Never raises.
    """
    if not settings.cloudbeds_api_key:
        return {"success": False, "error": "Cloudbeds API key not configured."}

    form: dict[str, str] = {
        "propertyID": settings.cloudbeds_property_id,
        "startDate": check_in,
        "endDate": check_out,
        "guestFirstName": first_name,
        "guestLastName": last_name,
        "guestEmail": email,
        "guestCountry": country,
        "guestZip": zip_code,
        "rooms[0][roomTypeID]": room_type_id,
        "rooms[0][quantity]": "1",
        # adults / children are partitioned by roomTypeID (NOT indexed by
        # room): each entry pairs a roomTypeID with a quantity. For multi-
        # room bookings spanning multiple room types you'd add more entries.
        "adults[0][roomTypeID]": room_type_id,
        "adults[0][quantity]": str(adults),
        "children[0][roomTypeID]": room_type_id,
        "children[0][quantity]": str(children),
        "sendEmailConfirmation": "false",
        # Cloudbeds requires paymentMethod even though GX-26 historically omits
        # it. Using "cash" as the placeholder for v1 (no payment wired yet);
        # when Stripe wiring lands we'll switch to "credit" + cardToken.
        "paymentMethod": "cash",
    }
    # sourceID (not sourceName — the latter is silently ignored by
    # postReservation; verified empirically). If not configured, Cloudbeds
    # defaults to "Website/Booking Engine" (s-1).
    if settings.cloudbeds_reservation_source_id:
        form["sourceID"] = settings.cloudbeds_reservation_source_id
    log.info("create_reservation: sourceID setting=%r form.sourceID=%r",
             settings.cloudbeds_reservation_source_id, form.get("sourceID"))
    if phone:
        form["guestPhone"] = phone
    if estimated_arrival_time:
        form["estimatedArrivalTime"] = estimated_arrival_time

    url = f"{CLOUDBEDS_BASE_URL}/postReservation"
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.post(url, data=form, headers=_auth_headers())
    except httpx.TimeoutException:
        log.warning("Cloudbeds postReservation timed out after %ss", DEFAULT_TIMEOUT_SECONDS)
        return {"success": False, "error": "Reservation creation timed out."}
    except httpx.HTTPError as e:
        log.warning("Cloudbeds postReservation HTTP error: %s", e)
        return {"success": False, "error": str(e)}

    if response.status_code != 200:
        log.warning(
            "Cloudbeds postReservation HTTP %s: %s",
            response.status_code, response.text[:300],
        )
        return {"success": False, "error": f"HTTP {response.status_code}: {response.text[:200]}"}

    body = response.json()
    if not body.get("success", False):
        log.warning("Cloudbeds postReservation success=false: %s", str(body)[:500])
        return {"success": False, "error": body.get("message") or "Cloudbeds rejected the reservation."}

    # postReservation puts reservation fields at the top level of the response
    # body, NOT nested under "data" like getReservation/getReservations.
    reservation_id = body.get("reservationID") or body.get("reservationId")
    if reservation_id is None:
        log.warning("create_reservation: success=true but no reservationID in body=%s", str(body)[:1000])
        return {"success": False, "error": "Cloudbeds returned success but no reservation ID."}

    log.info("create_reservation: created reservation_id=%s for %s %s status=%s total=%s",
             reservation_id, first_name, last_name, body.get("status"), body.get("grandTotal"))
    return {
        "success": True,
        "reservation_id": reservation_id,
        "status": body.get("status"),
        "grand_total": body.get("grandTotal"),
        "guest_id": body.get("guestID"),
    }


async def modify_reservation(
    reservation_id: str,
    *,
    new_check_out: str | None = None,
    estimated_arrival_time: str | None = None,
) -> dict:
    """Modify an existing reservation via Cloudbeds putReservation.

    Supported changes (v1):
    - `new_check_out` (ISO YYYY-MM-DD): change the check-out date. Used for
      extending or shortening a stay. Cloudbeds applies it across the whole
      reservation via the `checkoutDate` form field.
    - `estimated_arrival_time` ("HH:MM" 24h): update the expected arrival.

    Check-IN date changes are NOT supported in v1 — Cloudbeds doesn't expose
    a top-level checkInDate field on putReservation; doing it cleanly
    requires either rebuilding the rooms[] array or cancel-and-recreate.
    Direct-booking gating is the caller's responsibility (we don't check
    is_direct_booking here).

    Returns {"success": True, ...} or {"success": False, "error": "..."}.
    """
    if not settings.cloudbeds_api_key:
        return {"success": False, "error": "Cloudbeds API key not configured."}
    if not new_check_out and not estimated_arrival_time:
        return {"success": False, "error": "Nothing to modify — provide new_check_out or estimated_arrival_time."}

    form: dict[str, str] = {
        "propertyID": settings.cloudbeds_property_id,
        "reservationID": reservation_id,
    }
    if new_check_out:
        form["checkoutDate"] = new_check_out
    if estimated_arrival_time:
        form["estimatedArrivalTime"] = estimated_arrival_time

    url = f"{CLOUDBEDS_BASE_URL}/putReservation"
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.put(url, data=form, headers=_auth_headers())
    except httpx.TimeoutException:
        log.warning("Cloudbeds putReservation modify timed out")
        return {"success": False, "error": "Modification timed out."}
    except httpx.HTTPError as e:
        log.warning("Cloudbeds putReservation modify HTTP error: %s", e)
        return {"success": False, "error": str(e)}

    if response.status_code != 200:
        log.warning(
            "Cloudbeds putReservation modify HTTP %s: %s",
            response.status_code, response.text[:300],
        )
        return {"success": False, "error": f"HTTP {response.status_code}: {response.text[:200]}"}

    body = response.json()
    if not body.get("success", False):
        log.warning("Cloudbeds putReservation modify success=false: %s", str(body)[:500])
        return {"success": False, "error": body.get("message") or "Cloudbeds rejected the modification."}

    log.info(
        "modify_reservation: updated %s (new_check_out=%s eta=%s)",
        reservation_id, new_check_out, estimated_arrival_time,
    )
    return {"success": True, "reservation_id": reservation_id}


async def cancel_reservation(reservation_id: str, reason: str | None = None) -> dict:
    """Cancel an existing reservation via Cloudbeds putReservation (status=canceled).

    The optional `reason` is attached as a reservation note before the cancel
    so the rationale is preserved on the record. Direct-booking gating is
    the caller's responsibility — this function does not check is_direct_booking.

    Returns {"success": True} or {"success": False, "error": "..."}.
    """
    if not settings.cloudbeds_api_key:
        return {"success": False, "error": "Cloudbeds API key not configured."}

    if reason:
        note_result = await add_reservation_note(
            reservation_id, f"Cancellation reason: {reason}"
        )
        if not note_result.get("success"):
            # Don't fail the cancel just because the note didn't stick.
            log.warning(
                "cancel_reservation: reason-note failed (continuing): %s",
                note_result.get("error"),
            )

    form: dict[str, str] = {
        "propertyID": settings.cloudbeds_property_id,
        "reservationID": reservation_id,
        "status": "canceled",
    }
    url = f"{CLOUDBEDS_BASE_URL}/putReservation"
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.put(url, data=form, headers=_auth_headers())
    except httpx.TimeoutException:
        log.warning("Cloudbeds putReservation cancel timed out")
        return {"success": False, "error": "Cancellation timed out."}
    except httpx.HTTPError as e:
        log.warning("Cloudbeds putReservation cancel HTTP error: %s", e)
        return {"success": False, "error": str(e)}

    if response.status_code != 200:
        log.warning(
            "Cloudbeds putReservation cancel HTTP %s: %s",
            response.status_code, response.text[:300],
        )
        return {"success": False, "error": f"HTTP {response.status_code}: {response.text[:200]}"}

    body = response.json()
    if not body.get("success", False):
        log.warning("Cloudbeds putReservation cancel success=false: %s", str(body)[:500])
        return {"success": False, "error": body.get("message") or "Cloudbeds rejected the cancellation."}

    log.info("cancel_reservation: canceled reservation_id=%s", reservation_id)
    return {"success": True, "reservation_id": reservation_id}


async def add_reservation_note(reservation_id: str, note: str) -> dict:
    """Append a note to an existing reservation via postReservationNote.

    Returns {"success": True} or {"success": False, "error": "..."}. Never raises.
    """
    if not settings.cloudbeds_api_key:
        return {"success": False, "error": "Cloudbeds API key not configured."}

    form: dict[str, str] = {
        "propertyID": settings.cloudbeds_property_id,
        "reservationID": reservation_id,
        "reservationNote": note,
    }
    if settings.cloudbeds_iris_user_id:
        form["userID"] = settings.cloudbeds_iris_user_id
    url = f"{CLOUDBEDS_BASE_URL}/postReservationNote"
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.post(url, data=form, headers=_auth_headers())
    except httpx.TimeoutException:
        log.warning("Cloudbeds postReservationNote timed out")
        return {"success": False, "error": "Note add timed out."}
    except httpx.HTTPError as e:
        log.warning("Cloudbeds postReservationNote HTTP error: %s", e)
        return {"success": False, "error": str(e)}

    if response.status_code != 200:
        log.warning("Cloudbeds postReservationNote HTTP %s: %s",
                    response.status_code, response.text[:300])
        return {"success": False, "error": f"HTTP {response.status_code}: {response.text[:200]}"}

    body = response.json()
    if not body.get("success", False):
        log.warning("Cloudbeds postReservationNote success=false: %s", str(body)[:500])
        return {"success": False, "error": body.get("message") or "Cloudbeds rejected the note."}

    log.info("add_reservation_note: added note to reservation %s", reservation_id)
    return {"success": True}


async def mark_reservation_checked_out(reservation_id: str) -> bool:
    """Set a reservation status to checked-out (FUTURE workflow)."""
    log.info(f"[STUB] mark_reservation_checked_out({reservation_id})")
    return False


async def generate_payment_link(reservation_id: str) -> str | None:
    """Generate a Cloudbeds-hosted payment link for an existing reservation."""
    log.info(f"[STUB] generate_payment_link({reservation_id})")
    return None


async def list_sources() -> list[dict] | None:
    """List Cloudbeds reservation sources for this property.

    Used to look up sourceIDs (for create_reservation source attribution).
    getSources returns `data` as a list-of-lists (outer list = one entry per
    queried property, inner list = sources). We flatten across properties
    since this client is single-property.
    """
    body = await _get("getSources", params={"propertyIDs": settings.cloudbeds_property_id})
    if not body:
        return None
    data = body.get("data")
    if isinstance(data, list):
        sources: list[dict] = []
        for entry in data:
            if isinstance(entry, list):
                sources.extend(s for s in entry if isinstance(s, dict))
            elif isinstance(entry, dict):
                sources.append(entry)
        return sources
    log.warning("list_sources: unexpected data type %s, body=%s", type(data).__name__, str(body)[:500])
    return None


async def list_users() -> list[dict] | None:
    """List Cloudbeds users for this property.

    Used to look up userIDs (for note attribution, etc.). getUsers returns
    `data` as a dict keyed by propertyID with user-list values; we flatten
    across all properties since this client is single-property.
    """
    body = await _get("getUsers", params={"property_ids": settings.cloudbeds_property_id})
    if not body:
        return None
    data = body.get("data")
    if isinstance(data, dict):
        users: list[dict] = []
        for v in data.values():
            if isinstance(v, list):
                users.extend(v)
        return users
    if isinstance(data, list):
        return data
    log.warning("list_users: unexpected data type %s, body=%s", type(data).__name__, str(body)[:500])
    return None
