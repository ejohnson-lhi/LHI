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


import phonenumbers


def _looks_like_junk_phone(digits: str) -> bool:
    """Reject sequences that parse OK but are obviously not real numbers:
    all the same digit (5555555555), trivially short (<7 digits), or stuck
    at zero. Belt-and-suspenders -- libphonenumber already rejects most
    bad NANP numbers, but international validation is looser and these
    patterns are never legitimate."""
    if len(digits) < 7:
        return True
    if len(set(digits)) == 1:  # 1111111111, 0000000000
        return True
    return False


def normalize_phone_e164(raw: str | None, default_country_code: str = "1") -> str | None:
    """Normalize to E.164 (+15417295563) using libphonenumber. Returns None if
    the input doesn't parse to a valid number for any region, or trips the
    junk-pattern filter (all-same-digit, etc.).

    The `default_country_code` parameter is preserved for back-compat -- only
    "1" (US/NANP, the property's region) is honored. Numbers typed with a
    leading "+" are parsed against any region.
    """
    if not raw:
        return None
    raw = str(raw).strip()
    if not raw:
        return None

    # Cheap junk filter before invoking phonenumbers -- catches "1", "0000000",
    # "9999999999", etc. without paying for a parse.
    digits_only = re.sub(r"\D", "", raw)
    if _looks_like_junk_phone(digits_only):
        return None

    region = "US" if default_country_code == "1" else None
    try:
        parsed = phonenumbers.parse(raw, None if raw.startswith("+") else region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def format_phone_display(raw: str | None) -> str:
    """Pretty-print a phone using our house style:

    NANP (+1, 10 nat digits): "(541)-555-7890"
    Other:                    "+44-7911-123-456" (country code, then national
                              digits in groups of 3 with the last group
                              taking 3-4 digits)

    Validation goes through libphonenumber (normalize_phone_e164). On invalid
    input we return what the guest typed unchanged -- pre-fill stays editable
    so they can fix the typo rather than having their input vanish.
    """
    if not raw:
        return ""
    e164 = normalize_phone_e164(raw)
    if not e164:
        return str(raw).strip()
    # Parse once more to split country code from national number cleanly.
    try:
        parsed = phonenumbers.parse(e164, None)
    except phonenumbers.NumberParseException:
        return e164  # shouldn't happen -- e164 came from libphonenumber
    cc = str(parsed.country_code)
    nat = str(parsed.national_number)

    # NANP -- the property's home region.
    if cc == "1" and len(nat) == 10:
        return f"({nat[0:3]})-{nat[3:6]}-{nat[6:10]}"

    # Everything else: hyphen-separated groups of 3, with a trailing
    # 1-digit group merged into the previous one to avoid orphan digits
    # ("+44-791-112-345-6" -> "+44-791-112-3456").
    if not nat:
        return f"+{cc}"
    groups: list[str] = []
    i = 0
    while i < len(nat):
        groups.append(nat[i:i + 3])
        i += 3
    if len(groups) >= 2 and len(groups[-1]) == 1:
        groups[-2] += groups[-1]
        groups.pop()
    return f"+{cc}-" + "-".join(groups)


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
    # Cloudbeds returns guest contact fields either at the top level (newer
    # endpoints) or nested in guestList (older shape). Try top-level first,
    # fall back to the first guest in the list -- mirrors the GX-26 wrapper's
    # well-tested behavior. Same fallback used for guestID.
    guest_id = reservation.get("guestID") or ""
    main_guest: dict | None = None
    guest_list = reservation.get("guestList") or {}
    if isinstance(guest_list, dict):
        for gid, g in guest_list.items():
            if isinstance(g, dict):
                if not guest_id:
                    guest_id = gid
                if main_guest is None:
                    main_guest = g
                if g.get("isMainGuest"):
                    main_guest = g
                    break

    def _g(field: str) -> str | None:
        """Read a guest field: top-level first, then main_guest."""
        v = reservation.get(field)
        if v:
            return v
        if main_guest is not None:
            v = main_guest.get(field)
            if v:
                return v
        return None

    return {
        "reservation_id": reservation.get("reservationID"),
        "guest_id": guest_id or None,
        "guest_name": reservation.get("guestName"),
        "guest_first_name": _g("guestFirstName"),
        "guest_last_name": _g("guestLastName"),
        "guest_email": _g("guestEmail"),
        # NOTE Cloudbeds asymmetry: read as guestAddress, write as guestAddress1.
        "guest_address": _g("guestAddress"),
        "guest_address2": _g("guestAddress2"),
        "guest_city": _g("guestCity"),
        "guest_state": _g("guestState"),
        "guest_zip": _g("guestZip"),
        "guest_country": _g("guestCountry"),
        "guest_phone": _g("guestPhone"),
        "guest_cell_phone": _g("guestCellPhone"),
        "check_in": _format_iso_date_long(start_iso),
        "check_out": _format_iso_date_long(end_iso),
        "start_iso": start_iso,
        "end_iso": end_iso,
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
        # cards_on_file shape: list of {"cardID": "...", "cardNumber": "1234", "cardType": "visa"}.
        # cardNumber is last 4 only (PCI-safe). Empty list if no cards.
        "cards_on_file": reservation.get("cardsOnFile") or [],
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


async def put_guest_contact(
    *,
    guest_id: str,
    address1: str | None = None,
    address2: str | None = None,
    city: str | None = None,
    state: str | None = None,
    zip_code: str | None = None,
    country: str | None = None,
    phone: str | None = None,
    cell_phone: str | None = None,
    email: str | None = None,
) -> dict:
    """Update guest contact fields via Cloudbeds putGuest.

    Only fields passed as non-None are sent -- omitted keys leave whatever
    Cloudbeds currently has untouched. (Sending an empty string would CLEAR
    the field on their side.) Returns {"success": True} or
    {"success": False, "error": "..."}. Never raises.

    Field-name note: Cloudbeds is asymmetric -- reads expose `guestAddress`,
    writes use `guestAddress1`. Same with guestAddress2 for an optional
    second address line. Verified against the GX-26 B4J wrapper that's been
    in production against this property.
    """
    if not settings.cloudbeds_api_key:
        return {"success": False, "error": "Cloudbeds API key not configured."}
    if not guest_id:
        return {"success": False, "error": "guest_id is required."}

    # Cloudbeds form-encoded API quirk: sending guestX="" is SILENTLY IGNORED
    # (field keeps prior value). Sending guestX=" " (single space) stores an
    # empty string -- i.e., actually clears the field. Empirically verified
    # 2026-05-21 against putGuest. So caller passes "" to mean "clear", and
    # we translate to a single space on the wire.
    def _cb_value(v: str | None) -> str | None:
        if v is None:
            return None  # caller said "don't update"
        return " " if v == "" else v  # "" -> " " (clear); otherwise verbatim

    form: dict[str, str] = {
        "propertyID": settings.cloudbeds_property_id,
        "guestID": guest_id,
    }
    if (v := _cb_value(address1)) is not None: form["guestAddress1"] = v
    if (v := _cb_value(address2)) is not None: form["guestAddress2"] = v
    if (v := _cb_value(city)) is not None: form["guestCity"] = v
    if (v := _cb_value(state)) is not None: form["guestState"] = v
    if (v := _cb_value(zip_code)) is not None: form["guestZip"] = v
    if (v := _cb_value(country)) is not None: form["guestCountry"] = v
    if (v := _cb_value(phone)) is not None: form["guestPhone"] = v
    if (v := _cb_value(cell_phone)) is not None: form["guestCellPhone"] = v
    if (v := _cb_value(email)) is not None: form["guestEmail"] = v

    if len(form) <= 2:  # only propertyID + guestID -> nothing to update
        return {"success": False, "error": "No fields to update."}

    url = f"{CLOUDBEDS_BASE_URL}/putGuest"
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.put(url, data=form, headers=_auth_headers())
    except httpx.TimeoutException:
        log.warning("Cloudbeds putGuest timed out")
        return {"success": False, "error": "Update timed out."}
    except httpx.HTTPError as e:
        log.warning("Cloudbeds putGuest HTTP error: %s", e)
        return {"success": False, "error": str(e)}

    if response.status_code != 200:
        log.warning("Cloudbeds putGuest HTTP %s: %s", response.status_code, response.text[:300])
        return {"success": False, "error": f"HTTP {response.status_code}: {response.text[:200]}"}

    body = response.json()
    if not body.get("success", False):
        log.warning("Cloudbeds putGuest success=false: %s", str(body)[:500])
        return {"success": False, "error": body.get("message") or "Cloudbeds rejected the update."}

    log.info("put_guest_contact: updated guest %s (fields=%s)", guest_id, sorted(form.keys()))
    return {"success": True, "guest_id": guest_id}


async def post_item(reservation_id: str, item_id: str, quantity: int = 1) -> dict:
    """Add a fee/item line to a reservation's folio via Cloudbeds postItem.

    Returns {"success": True, "sold_product_id": "..."} or
    {"success": False, "error": "..."}. The soldProductID is what you'd
    pass to postVoidItem later to remove this charge.

    `quantity` multiplies the per-unit cost configured for the item in
    Cloudbeds (so itemID=dog-fee + quantity=2 charges 2 x the unit price).
    """
    if not settings.cloudbeds_api_key:
        return {"success": False, "error": "Cloudbeds API key not configured."}
    if not item_id:
        return {"success": False, "error": "item_id is required."}
    if quantity < 1:
        return {"success": False, "error": "quantity must be >= 1."}
    form = {
        "propertyID": settings.cloudbeds_property_id,
        "reservationID": reservation_id,
        "itemID": item_id,
        "itemQuantity": str(quantity),
    }
    url = f"{CLOUDBEDS_BASE_URL}/postItem"
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.post(url, data=form, headers=_auth_headers())
    except httpx.TimeoutException:
        log.warning("Cloudbeds postItem timed out (res=%s item=%s)", reservation_id, item_id)
        return {"success": False, "error": "Charge add timed out."}
    except httpx.HTTPError as e:
        log.warning("Cloudbeds postItem HTTP error: %s", e)
        return {"success": False, "error": str(e)}
    if response.status_code != 200:
        log.warning("Cloudbeds postItem HTTP %s: %s", response.status_code, response.text[:300])
        return {"success": False, "error": f"HTTP {response.status_code}: {response.text[:200]}"}
    body = response.json()
    if not body.get("success", False):
        log.warning("Cloudbeds postItem success=false: %s", str(body)[:500])
        return {"success": False, "error": body.get("message") or "Cloudbeds rejected the item."}
    sold_id = (body.get("data") or {}).get("soldProductID") or body.get("soldProductID") or ""
    log.info("post_item: res=%s item=%s qty=%d -> sold_product_id=%s",
             reservation_id, item_id, quantity, sold_id)
    return {"success": True, "sold_product_id": sold_id}


def detect_card_type_from_pan(pan: str) -> str | None:
    """Identify the card brand from the leading digits (BIN ranges).
    Returns one of 'visa' / 'mastercard' / 'amex' / 'discover' / 'diners' /
    'jcb', or None when unrecognized. Used so we can populate Cloudbeds'
    cardType field even when the caller hasn't passed it -- matches GX-26
    which always sends a brand."""
    if not pan or not pan.isdigit():
        return None
    if pan.startswith("4"):
        return "visa"
    if pan[:2] in {"51", "52", "53", "54", "55"} or (len(pan) >= 4 and 2221 <= int(pan[:4]) <= 2720):
        return "mastercard"
    if pan[:2] in {"34", "37"}:
        return "amex"
    if pan.startswith("6011") or pan.startswith("65") or pan[:3] in {"644", "645", "646", "647", "648", "649"}:
        return "discover"
    if pan[:2] in {"36", "38", "39"} or pan[:3] in {"300", "301", "302", "303", "304", "305"}:
        return "diners"
    if pan.startswith("35"):
        return "jcb"
    return None


async def post_credit_card(
    reservation_id: str,
    *,
    # Raw-card path (the GX-26-proven flow). Cloudbeds tokenizes internally
    # against their Stripe Connect account. Cards on properties using the
    # StripePlatformGateway with cloudbedsPayments=True don't need a Stripe
    # SDK on our side -- Cloudbeds owns the merchant relationship.
    card_number: str | None = None,
    card_expiration: str | None = None,   # "MM/YY", e.g. "09/27"
    card_cvv: str | None = None,
    card_holder_name: str | None = None,
    card_type: str | None = None,
    card_address_zip: str | None = None,
    # Stripe SDK path (alternate). Use only if the property exposes a Stripe
    # Elements / Payment Element setup tied to their Connect Account.
    payment_method_id: str | None = None,
    return_url: str | None = None,
) -> dict:
    """Attach a credit card to a Cloudbeds reservation via postCreditCard.
    Two mutually-exclusive paths:
      * Raw-card fields (card_number + card_expiration + card_cvv + holder)
        -- Cloudbeds tokenizes internally. PCI scope sits on our backend
        during the in-flight POST only. NEVER LOGGED, never persisted.
      * payment_method_id from Stripe.js -- when a Stripe SDK is wired up.

    Returns {"success": True, "card_id": "..."} or
    {"success": False, "error": "...", "requires_3ds": bool, "redirect_url": "..."}.

    Caller MUST scrub the raw values from local variables after this returns
    (Python's GC will eventually, but explicit is safer for audit purposes).
    """
    if not settings.cloudbeds_api_key:
        return {"success": False, "error": "Cloudbeds API key not configured."}
    form = {
        "propertyID": settings.cloudbeds_property_id,
        "reservationID": reservation_id,
    }
    if payment_method_id:
        form["paymentMethodId"] = payment_method_id
    elif card_number and card_expiration and card_cvv:
        form["cardNumber"] = card_number
        form["cardExpiration"] = card_expiration
        form["cardCvv"] = card_cvv
        if card_holder_name:
            form["cardHolderName"] = card_holder_name
        if card_type:
            form["cardType"] = card_type
        if card_address_zip:
            form["cardAddressZip"] = card_address_zip
    else:
        return {"success": False, "error": "Either raw card fields or payment_method_id is required."}
    if return_url:
        form["returnUrl"] = return_url
    url = f"{CLOUDBEDS_BASE_URL}/postCreditCard"
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.post(url, data=form, headers=_auth_headers())
    except httpx.TimeoutException:
        log.warning("Cloudbeds postCreditCard timed out (res=%s)", reservation_id)
        return {"success": False, "error": "Card attach timed out."}
    except httpx.HTTPError as e:
        log.warning("Cloudbeds postCreditCard HTTP error: %s", e)
        return {"success": False, "error": str(e)}
    if response.status_code != 200:
        log.warning("Cloudbeds postCreditCard HTTP %s body=%s sent_keys=%s",
                    response.status_code, response.text[:500], sorted(form.keys()))
        return {"success": False, "error": f"HTTP {response.status_code}: {response.text[:200]}"}
    body = response.json()
    if not body.get("success", False):
        # Log FULL body + the keys we sent (NOT values -- PCI) so we can
        # diagnose what Cloudbeds actually objected to. Their public message
        # is often just "An unexpected error occurred"; detail is in `data`
        # or `errors` if present.
        log.warning(
            "Cloudbeds postCreditCard FAIL body=%s sent_keys=%s",
            str(body)[:1000], sorted(form.keys()),
        )
        # 3DS challenge surfaces as a redirect URL in the response in some
        # cases -- forward it so the caller can navigate the guest there.
        data = body.get("data") or {}
        return {
            "success": False,
            "error": body.get("message") or "Cloudbeds rejected the card.",
            "requires_3ds": bool(data.get("redirectUrl")),
            "redirect_url": data.get("redirectUrl"),
        }
    data = body.get("data") or {}
    card_id = data.get("cardID") or data.get("cardId") or ""
    log.info("post_credit_card: res=%s card_id=%s last4=%s",
             reservation_id, card_id, data.get("cardNumber", "?"))
    return {"success": True, "card_id": str(card_id)}


async def post_reservation_document(
    reservation_id: str,
    pdf_bytes: bytes,
    filename: str,
) -> dict:
    """Attach a PDF file to a Cloudbeds reservation via the
    postReservationDocument endpoint (multipart upload).

    Returns {"success": True, "doc_id": "..."} or
    {"success": False, "error": "..."}. The doc_id is whatever Cloudbeds
    returns (varies by API version) and is stored for future reference.
    """
    if not settings.cloudbeds_api_key:
        return {"success": False, "error": "Cloudbeds API key not configured."}
    if not pdf_bytes:
        return {"success": False, "error": "Empty PDF payload."}
    url = f"{CLOUDBEDS_BASE_URL}/postReservationDocument"
    # propertyID + reservationID go in the form, the PDF goes as the 'file'
    # multipart part. httpx assembles it correctly when we use files=.
    data = {
        "propertyID": settings.cloudbeds_property_id,
        "reservationID": reservation_id,
    }
    files = {"file": (filename, pdf_bytes, "application/pdf")}
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.post(url, data=data, files=files, headers=_auth_headers())
    except httpx.TimeoutException:
        log.warning("Cloudbeds postReservationDocument timed out (res=%s)", reservation_id)
        return {"success": False, "error": "Upload timed out."}
    except httpx.HTTPError as e:
        log.warning("Cloudbeds postReservationDocument HTTP error: %s", e)
        return {"success": False, "error": str(e)}
    if response.status_code != 200:
        log.warning("Cloudbeds postReservationDocument HTTP %s: %s",
                    response.status_code, response.text[:300])
        return {"success": False, "error": f"HTTP {response.status_code}: {response.text[:200]}"}
    body = response.json()
    if not body.get("success", False):
        log.warning("Cloudbeds postReservationDocument success=false: %s", str(body)[:500])
        return {"success": False, "error": body.get("message") or "Cloudbeds rejected the upload."}
    # Various API versions return different identifier shapes; we just
    # forward whatever's in the response for later debugging.
    doc_id = (body.get("data") or {}).get("id") or body.get("documentID") or ""
    log.info("post_reservation_document: res=%s file=%s bytes=%d doc_id=%s",
             reservation_id, filename, len(pdf_bytes), doc_id)
    return {"success": True, "doc_id": str(doc_id)}


async def post_void_item(reservation_id: str, sold_product_id: str) -> dict:
    """Void a previously-posted item via Cloudbeds postVoidItem.

    Returns {"success": True} or {"success": False, "error": "..."}.
    Voiding a non-existent / already-voided ID returns success=false from
    Cloudbeds; callers can usually treat that as best-effort cleanup and
    continue (we surface the error string for inspection).
    """
    if not settings.cloudbeds_api_key:
        return {"success": False, "error": "Cloudbeds API key not configured."}
    if not sold_product_id:
        return {"success": False, "error": "sold_product_id is required."}
    form = {
        "propertyID": settings.cloudbeds_property_id,
        "reservationID": reservation_id,
        "soldProductID": sold_product_id,
    }
    url = f"{CLOUDBEDS_BASE_URL}/postVoidItem"
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.post(url, data=form, headers=_auth_headers())
    except httpx.TimeoutException:
        log.warning("Cloudbeds postVoidItem timed out (res=%s sold=%s)", reservation_id, sold_product_id)
        return {"success": False, "error": "Void timed out."}
    except httpx.HTTPError as e:
        log.warning("Cloudbeds postVoidItem HTTP error: %s", e)
        return {"success": False, "error": str(e)}
    if response.status_code != 200:
        log.warning("Cloudbeds postVoidItem HTTP %s: %s", response.status_code, response.text[:300])
        return {"success": False, "error": f"HTTP {response.status_code}: {response.text[:200]}"}
    body = response.json()
    if not body.get("success", False):
        log.warning("Cloudbeds postVoidItem success=false (res=%s sold=%s): %s",
                    reservation_id, sold_product_id, str(body)[:300])
        return {"success": False, "error": body.get("message") or "Void rejected by Cloudbeds."}
    log.info("post_void_item: res=%s sold=%s OK", reservation_id, sold_product_id)
    return {"success": True}


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
