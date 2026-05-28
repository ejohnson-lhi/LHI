"""Cloudbeds internal-dashboard API client.

Uses session cookies harvested from Playwright-driven Cloudbeds login to
call the dashboard's *internal* endpoints under hotels.cloudbeds.com.
The PUBLIC API at api.cloudbeds.com doesn't reliably expose card
attachment / payment ops under our API key scope.

**FOR REMAPPING WHEN CLOUDBEDS CHANGES THE SYSTEM**: see the project
memory file `project_cloudbeds_internal_dashboard_endpoints.md`. That
file documents every endpoint's URL, request shape, response shape,
auth model, and an exact playbook for diagnosing and fixing each likely
class of breakage (URL change, body shape change, auth change, etc.).

Trade-offs vs. the Playwright Add Card path:
  + Single HTTP call. ~1-2s end-to-end vs ~30s for browser automation.
  + No selector maintenance — the form body is fixed.
  + PCI-clean for card-capture: only the Stripe legacy token (an opaque
    `tok_xxx` reference) touches our backend, never the PAN.
  - Internal endpoints. Cloudbeds doesn't owe us stability. If they
    change a URL, body format, or auth mechanism, our calls break.
    Fallback path for card capture is the validated Playwright Add Card
    automation in cloudbeds_browser.py.
  - Still requires Playwright at least once per ~8h to bootstrap a valid
    session (Okta MFA isn't scriptable via httpx).
"""
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)

# Same path that cloudbeds_browser.py writes the Playwright storage_state to.
# Relative to CWD; resolves to /opt/iris-backend/backend/data/... on droplet.
_SESSION_CACHE_PATH = Path("data") / ".cloudbeds_session.json"

# Property-level constants captured from a working dashboard session
# (HAR data, 2026-05-25, property 176010). If we ever onboard another
# property these need to become per-property config — fetch via the
# dashboard's get_credit_card_info call which returns them under
# accountStatus.billing.
_BILLING_PORTAL_ID = "64628"
_IS_BP_SETUP_COMPLETED = "1"

# Dashboard's front-end version constants. Probably not actually
# validated by the backend, but include for fidelity. May need periodic
# updates if Cloudbeds tightens validation.
_FRONT_VERSION = "18.214.5"
_VERSION_URL = "https://front.cloudbeds.com/mfd-root/app.js"

_SAVE_CARD_URL = "https://hotels.cloudbeds.com/hotel/save_credit_card"
_PAYMENT_ADD_URL = "https://hotels.cloudbeds.com/connect/payment/add"
_DEFAULT_TIMEOUT_SECONDS = 30.0


# ──────────────────────────────────────────────────────────────────────────
# Booking-ID lookup
#
# Cloudbeds uses two distinct identifiers for a reservation:
#   - `reservationID` (e.g. "1989264686165") — 13-digit public ID,
#     what the public API takes/returns
#   - `booking_id`    (e.g. "175931510")     — 9-digit internal ID,
#     what the dashboard URL uses and what save_credit_card requires
#
# Confirmed 2026-05-25 the public-API surface offers NO way to derive
# booking_id from reservationID:
#   - getReservation: no bookingID at top level
#   - getReservation.cardsOnFile: only {cardID, cardNumber, cardType} —
#     the dashboard's internal /connect/CreditCard/list adds booking_id
#     but THAT endpoint itself requires booking_id to be called.
#
# Fast path: the dashboard's internal search endpoint
# /hotel/get_customer_reservation takes the public ID via the `query`
# field and returns the booking_id as `id` (with the public ID echoed
# back as `identifier`). Same auth pattern as save_credit_card.
# Playwright fallback exists for the rare case the session-cookie path
# is unavailable.
# ──────────────────────────────────────────────────────────────────────────


_SEARCH_URL = "https://hotels.cloudbeds.com/hotel/get_customer_reservation"


async def lookup_booking_id_via_search_api(
    reservation_id: str,
    *,
    property_id: str | None = None,
) -> str | None:
    """Fast path: call the dashboard's internal search endpoint with the
    public reservation ID, parse the internal booking_id (`id`) out of
    the result. Sub-second. Requires a valid Cloudbeds session in the
    cookie cache; returns None on session-missing / API-failure.

    Endpoint: POST https://hotels.cloudbeds.com/hotel/get_customer_reservation
    captured from HAR 2026-05-25 (the dashboard's own search box uses it).
    Result shape:
        {"success": true, "reservations": [
            {"id": "175931510", "identifier": "1989264686165", ...}
        ]}"""
    prop = property_id or settings.cloudbeds_property_id
    if not prop:
        return None

    cookies = _load_session_cookies()
    if not cookies:
        log.warning("lookup_booking_id_via_search_api: no session cookies on disk")
        return None
    csrf = cookies.get("csrf_accessa_cookie")
    if not csrf:
        log.warning("lookup_booking_id_via_search_api: missing csrf_accessa_cookie")
        return None

    form = {
        "query": str(reservation_id),
        "search_type": "local",
        "suppress_client_errors": "true",
        "property_id": str(prop),
        "group_id": str(prop),
        "version": _VERSION_URL,
        "frontVersion": _FRONT_VERSION,
        "csrf_accessa": csrf,
        "billing_portal_id": _BILLING_PORTAL_ID,
        "is_bp_setup_completed": _IS_BP_SETUP_COMPLETED,
    }
    headers = _build_headers(str(prop))

    try:
        async with httpx.AsyncClient(
            cookies=cookies,
            timeout=_DEFAULT_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            resp = await client.post(_SEARCH_URL, data=form, headers=headers)
    except httpx.HTTPError as ex:
        log.warning("lookup_booking_id_via_search_api: HTTP error: %s", ex)
        return None

    if resp.status_code != 200:
        log.warning("lookup_booking_id_via_search_api: HTTP %s: %s",
                    resp.status_code, resp.text[:300])
        return None

    try:
        body = resp.json()
    except ValueError:
        log.warning("lookup_booking_id_via_search_api: non-JSON response")
        return None

    if not body.get("success"):
        log.warning("lookup_booking_id_via_search_api: success=false: %s",
                    str(body)[:300])
        return None

    # Walk the results, matching on identifier to be safe. Cloudbeds'
    # `local` search type returns reservations on this property only,
    # and `query` is an exact ID match for full 13-digit reservation
    # numbers, so we expect exactly one result, but match defensively.
    target = str(reservation_id)
    for res in body.get("reservations") or []:
        if str(res.get("identifier")) == target:
            bid = res.get("id")
            if bid:
                return str(bid)
    log.info(
        "lookup_booking_id_via_search_api: no exact match for %s in %d result(s)",
        reservation_id, len(body.get("reservations") or []),
    )
    return None


async def lookup_booking_id_via_dashboard_search(reservation_id: str) -> str | None:
    """Slow-path fallback: drive the dashboard's search box with Playwright
    to find the booking_id for a fresh reservation that has no cards yet.
    Takes ~10-15 seconds (Playwright launch + login validation + search +
    extract). Use this only at mint time / webhook-receive time, never
    inside a guest's hot path.

    Returns the booking_id string from the search result link
    (`<a href="#/reservations/175931510" data-id="175931510">`), or None
    if not found."""
    from app.tools.cloudbeds_browser import CloudbedsBrowser
    async with CloudbedsBrowser() as cb:
        ok = await cb.login()
        if not ok:
            log.warning("lookup_booking_id_via_dashboard_search: login failed")
            return None
        # Already on the reservations list per login()'s entry-URL navigation.
        # If we're not, navigate explicitly.
        if "#/reservations" not in (cb.page.url or ""):
            target = (
                f"https://hotels.cloudbeds.com/connect/{settings.cloudbeds_property_id}"
                "#/reservations"
            )
            await cb.page.goto(target, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(1.5)
        # Type the public reservation ID into the search input
        ok = await cb._humanlike_type(
            reservation_id,
            "input[name='find_reservations']",
            timeout=10000,
            allow_typos=False,
            familiarity=1.0,
        )
        if not ok:
            log.warning("lookup_booking_id_via_dashboard_search: search input not reachable")
            return None
        await cb.page.locator("input[name='find_reservations']").first.press("Enter")
        await asyncio.sleep(2.0)
        # Result is a link like <a href="#/reservations/175931510" data-id="175931510">
        try:
            link = cb.page.locator("a.view_summary").first
            await link.wait_for(state="visible", timeout=10000)
            data_id = await link.get_attribute("data-id")
            if data_id:
                return str(data_id).strip()
            href = await link.get_attribute("href")
            if href and "/reservations/" in href:
                return href.split("/reservations/")[1].split("?")[0].split("#")[0].strip()
        except Exception as ex:
            log.warning("lookup_booking_id_via_dashboard_search: search result not parseable: %s", ex)
            return None
    return None


async def get_booking_id(reservation_id: str) -> str | None:
    """Resolve a public reservation ID to its internal booking_id.

    Fast path: dashboard's internal `get_customer_reservation` endpoint
    (httpx, ~500ms — needs a valid session in the cookie cache).
    Fallback: Playwright dashboard search (~10-15s) — only triggered when
    the fast path can't run (session missing / network blip).

    Either way, the result is appropriate to cache (e.g. on a
    portal_token row at mint time). The booking_id ↔ reservation_id
    mapping is permanent."""
    bid = await lookup_booking_id_via_search_api(reservation_id)
    if bid:
        log.info(
            "get_booking_id(%s) = %s (via search API, fast path)",
            reservation_id, bid,
        )
        return bid
    log.info(
        "get_booking_id(%s): fast path failed, falling back to Playwright search",
        reservation_id,
    )
    bid = await lookup_booking_id_via_dashboard_search(reservation_id)
    if bid:
        log.info(
            "get_booking_id(%s) = %s (via Playwright search fallback)",
            reservation_id, bid,
        )
    else:
        log.warning("get_booking_id(%s): both lookup paths failed", reservation_id)
    return bid


# ──────────────────────────────────────────────────────────────────────────
# Payment operations: Authorize (hold) + Purchase (capture/charge)
#
# Same endpoint /connect/payment/add for both — switching behavior on the
# `transaction_type` field inside the JSON-encoded `payment` form field.
# Body shape captured from HAR 2026-05-27. The dashboard sends both
# camelCase and snake_case variants of several fields (e.g. isAuthPayment
# + auth_payment) — looks like Cloudbeds is migrating from one convention
# to the other and supports both. We send both for compatibility.
#
# Authorize: places a hold on the card. No money moves. Holds typically
# expire after ~7 days at the issuing bank. Response has
# transaction_id: null, paid_value: "0.00".
#
# Purchase: actual charge — money moves to the merchant. Response has a
# real transaction_id and paid_value equal to the amount.
#
# 3DS handling: if the issuer requires extra verification, the response
# carries requires_authentication: true + authentication_url. The caller
# is expected to redirect the guest to that URL (typically a bank-hosted
# challenge page) — when they complete, the bank posts back and the
# transaction completes. For the voice/portal flow we'd surface this
# branch as "complete extra verification at <url>".
# ──────────────────────────────────────────────────────────────────────────


def _build_payment_form(
    *,
    booking_id: str,
    credit_card_id: str,
    amount: float,
    transaction_type: str,
    description: str,
    folio_id: str | None,
    property_id: str,
    csrf_accessa: str,
) -> dict[str, str]:
    """Build the form body for /connect/payment/add. The body has two
    JSON-encoded form fields (`payment`, `inventory_object`) plus the
    usual session/version/csrf scaffolding.

    The `payment` object's shape comes from HAR capture. Both auth-flag
    pairs (camelCase + snake_case) are populated so we don't trip a
    field-name validator if Cloudbeds is mid-migration."""
    is_authorize = transaction_type == "Authorize"
    payment_data: dict[str, Any] = {
        "amount": amount,
        "billing_details": {
            # All empty by default — Cloudbeds-hosted card on file already
            # has the cardholder info from the original save. Including the
            # full shape so the field-name validator doesn't reject.
            "address1": "", "address2": "", "city": "", "company": "",
            "country": "", "email": "", "name": "", "phone": "",
            "state": "", "zip": "", "first_name": "", "last_name": "",
            "document_number": "", "tax_id": "", "birthday": "",
            "document_type": "",
        },
        "card_number": "",                  # not used when paying with saved card
        "cardholder_name": "",              # ditto
        "credit_card_id": str(credit_card_id),
        "credit_card_type": None,
        "payment_method": "cards",
        "usePaymentFees": False,
        "use_payment_fees": False,
        "isAllocatePayment": False,
        "isAutoDate": True,
        "isDeposit": False,
        "allocate_payment": "0",
        "transaction_type": transaction_type,   # "Authorize" or "Purchase"
        "keep_credit_card_info": "1",
        "auth_payment": "1" if is_authorize else "0",
        "isAuthPayment": is_authorize,
        "auto_date": "1",
        "accountsReceivableLedgerId": "",
        "description": description,
        "folio_id": folio_id or "",
        "currency": "",
        "payment_date": "",
        "payment_type": "exist_credit_card",
        "keep_pending_payment": "0",
        "process_payment": "0" if is_authorize else "1",
        "isProcessPayment": not is_authorize,
        "property_id": str(property_id),
        "is_deposit": False,
        "isNextGenPayload": True,
    }
    return {
        "payment": json.dumps(payment_data),
        "inventory_object": json.dumps({
            "type": "reservation",
            "id": str(booking_id),
        }),
        "suppress_client_errors": "true",
        "property_id": str(property_id),
        "group_id": str(property_id),
        "version": _VERSION_URL,
        "frontVersion": _FRONT_VERSION,
        "csrf_accessa": csrf_accessa,
        "billing_portal_id": _BILLING_PORTAL_ID,
        "is_bp_setup_completed": _IS_BP_SETUP_COMPLETED,
    }


async def dashboard_payment_add(
    *,
    booking_id: str,
    credit_card_id: str,
    amount: float,
    transaction_type: str,
    description: str = "",
    folio_id: str | None = None,
    property_id: str | None = None,
) -> dict:
    """Create a payment transaction on a reservation via Cloudbeds'
    internal /connect/payment/add endpoint.

    Arguments:
      booking_id: internal Cloudbeds reservation ID (e.g. "175931510")
      credit_card_id: Cloudbeds-side card id (the `id` from the
        save_credit_card response, or from /connect/CreditCard/list, or
        from getReservation.cardsOnFile.cardID)
      amount: dollar amount as float (e.g. 10.0 for $10.00)
      transaction_type: "Authorize" (places a hold) or "Purchase"
        (captures/charges the card)
      description: free-text note attached to the transaction (shown in
        the dashboard's transaction list)
      folio_id: optional folio attribution. When the dashboard's
        Add/Refund Payment popin sends a transaction, it includes the
        reservation's folio id here. Omit to leave unattributed; the
        response will report folio_id="0".
      property_id: defaults to settings.cloudbeds_property_id

    Returns:
      On success: {
        "success": True,
        "payment_id": "228984481",          # Cloudbeds-internal payment row
        "transaction_id": "235604278..." or None (None for Authorize),
        "transaction_type": "Authorize" | "Purchase",
        "status": "successful",
        "amount": 10.0,
        "paid": 10.0,
        "currency": "USD",
        "date": "2026-05-27 16:20:36",
        "card_last4": "9675",
        "requires_authentication": False,
        "authentication_url": None,
      }
      On 3DS challenge: success=True with requires_authentication=True
      and authentication_url populated — caller redirects the guest.
      On failure: {"success": False, "error": "...", "detail": ...}
    """
    if transaction_type not in ("Authorize", "Purchase"):
        return {"success": False, "error": f"Invalid transaction_type: {transaction_type!r}. Must be 'Authorize' or 'Purchase'."}
    if amount <= 0:
        return {"success": False, "error": f"Amount must be positive, got {amount}."}

    prop = property_id or settings.cloudbeds_property_id
    if not prop:
        return {"success": False, "error": "No Cloudbeds property ID configured."}

    cookies = _load_session_cookies()
    if not cookies:
        return {
            "success": False,
            "error": "No Cloudbeds session cookies on disk. "
                     "Run scripts/test_cloudbeds_login.py to bootstrap the session.",
        }
    csrf = cookies.get("csrf_accessa_cookie")
    if not csrf:
        return {
            "success": False,
            "error": "Missing csrf_accessa_cookie in cached session.",
        }

    form = _build_payment_form(
        booking_id=booking_id,
        credit_card_id=credit_card_id,
        amount=amount,
        transaction_type=transaction_type,
        description=description,
        folio_id=folio_id,
        property_id=str(prop),
        csrf_accessa=csrf,
    )
    headers = _build_headers(str(prop))

    log.info(
        "dashboard_payment_add: %s amount=%s card_id=%s booking=%s",
        transaction_type, amount, credit_card_id, booking_id,
    )

    try:
        async with httpx.AsyncClient(
            cookies=cookies,
            timeout=_DEFAULT_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            resp = await client.post(_PAYMENT_ADD_URL, data=form, headers=headers)
    except httpx.TimeoutException:
        log.warning("dashboard_payment_add timed out (booking=%s)", booking_id)
        return {"success": False, "error": "Cloudbeds payment/add timed out."}
    except httpx.HTTPError as e:
        log.warning("dashboard_payment_add HTTP error: %s", e)
        return {"success": False, "error": f"HTTP error: {e}"}

    if resp.status_code != 200:
        log.warning("dashboard_payment_add HTTP %s: %s",
                    resp.status_code, resp.text[:500])
        return {
            "success": False,
            "error": f"HTTP {resp.status_code}",
            "detail": resp.text[:500],
        }

    try:
        body = resp.json()
    except ValueError:
        return {
            "success": False,
            "error": "Non-JSON response from Cloudbeds.",
            "detail": resp.text[:500],
        }

    if not body.get("success"):
        log.warning(
            "dashboard_payment_add success=false: %s",
            json.dumps(body)[:500],
        )
        return {
            "success": False,
            "error": body.get("message") or body.get("error") or "Cloudbeds rejected the payment.",
            "detail": body,
        }

    data = body.get("data") or {}
    requires_auth = bool(data.get("requires_authentication"))
    log.info(
        "dashboard_payment_add OK: payment_id=%s status=%s paid=%s txn=%s 3ds=%s",
        data.get("id"), data.get("status"), data.get("paid"),
        data.get("transaction_id"), requires_auth,
    )
    return {
        "success": True,
        "payment_id": str(data.get("id", "")),
        "transaction_id": data.get("transaction_id"),
        "transaction_type": data.get("transaction_type"),
        "status": data.get("status"),
        "amount": data.get("price"),
        "paid": data.get("paid"),
        "currency": data.get("currency"),
        "date": data.get("date"),
        "card_last4": data.get("card_number"),
        "requires_authentication": requires_auth,
        "authentication_url": data.get("authentication_url"),
    }


async def dashboard_authorize_card(
    booking_id: str,
    credit_card_id: str,
    amount: float,
    *,
    description: str = "",
    folio_id: str | None = None,
) -> dict:
    """Place an authorization hold on a saved card. No money moves.
    See dashboard_payment_add for full return shape."""
    return await dashboard_payment_add(
        booking_id=booking_id,
        credit_card_id=credit_card_id,
        amount=amount,
        transaction_type="Authorize",
        description=description,
        folio_id=folio_id,
    )


async def dashboard_capture_card(
    booking_id: str,
    credit_card_id: str,
    amount: float,
    *,
    description: str = "",
    folio_id: str | None = None,
) -> dict:
    """Charge a saved card (capture). Real money moves immediately.
    See dashboard_payment_add for full return shape.

    Note that Cloudbeds calls this "Purchase" in their API; the
    dashboard's button labels say "Charge" / "Capture". Same operation."""
    return await dashboard_payment_add(
        booking_id=booking_id,
        credit_card_id=credit_card_id,
        amount=amount,
        transaction_type="Purchase",
        description=description,
        folio_id=folio_id,
    )

# Exact list of token.card sub-fields that the dashboard sends, in the
# order the HAR showed. Cloudbeds sends every field including empties —
# we replicate that so the server-side validator sees the shape it
# expects.
_TOKEN_CARD_FIELDS = (
    "id", "object",
    "address_city", "address_country", "address_line1", "address_line1_check",
    "address_line2", "address_state", "address_zip", "address_zip_check",
    "brand", "country", "cvc_check", "dynamic_last4", "email",
    "exp_month", "exp_year", "funding", "last4", "name", "phone",
    "regulated_status", "tokenization_method", "use", "wallet",
)


def _load_session_cookies() -> dict[str, str]:
    """Load Cloudbeds session cookies from the Playwright storage_state
    cache. Filters to cookies on *.cloudbeds.com so unrelated Okta /
    third-party cookies captured during the OAuth dance don't leak into
    our outbound request. Returns {name: value}."""
    if not _SESSION_CACHE_PATH.exists():
        log.warning("No Cloudbeds session cache at %s — run a Playwright login first.",
                    _SESSION_CACHE_PATH)
        return {}
    try:
        with _SESSION_CACHE_PATH.open("r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception as ex:
        log.warning("Couldn't read Cloudbeds session cache: %s", ex)
        return {}
    cookies: dict[str, str] = {}
    for c in state.get("cookies", []):
        domain = c.get("domain", "")
        if "cloudbeds.com" in domain:
            cookies[c["name"]] = c["value"]
    return cookies


def _build_save_card_form(
    *,
    booking_id: str,
    property_id: str,
    legacy_token_id: str,
    token_card: dict[str, Any],
    csrf_accessa: str,
) -> dict[str, str]:
    """Construct the form body for `save_credit_card`. Mirrors what
    Cloudbeds' dashboard JS sends — every field, in roughly the order
    captured from HAR (order shouldn't matter for form-encoded but we
    don't take chances). Empty values stay as empty strings."""
    form: dict[str, str] = {
        "is_active": "1",
        "token_data": legacy_token_id,
    }
    # token_card[*] entries using PHP-style array notation. httpx will
    # url-encode the brackets correctly.
    for field in _TOKEN_CARD_FIELDS:
        v = token_card.get(field)
        form[f"token_card[{field}]"] = "" if v is None else str(v)
    # token_card.networks.preferred — nested key
    networks = token_card.get("networks") or {}
    preferred = networks.get("preferred")
    form["token_card[networks][preferred]"] = "" if preferred is None else str(preferred)
    # The rest of the body
    form.update({
        "group_profile_id": "0",
        "booking_id": str(booking_id),
        "suppress_client_errors": "true",
        "property_id": str(property_id),
        "group_id": str(property_id),
        "version": _VERSION_URL,
        "frontVersion": _FRONT_VERSION,
        "csrf_accessa": csrf_accessa,
        "billing_portal_id": _BILLING_PORTAL_ID,
        "is_bp_setup_completed": _IS_BP_SETUP_COMPLETED,
    })
    return form


def _build_headers(property_id: str) -> dict[str, str]:
    """Build request headers that mirror what the dashboard sends. The
    important bits are X-Requested-With (CSRF-style ajax marker),
    Origin/Referer (Cloudbeds checks these to refuse cross-origin
    forgery), and x-used-method (internal Cloudbeds marker)."""
    return {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://hotels.cloudbeds.com",
        "Referer": f"https://hotels.cloudbeds.com/connect/{property_id}",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "X-Property-Id": str(property_id),
        "X-Requested-With": "XMLHttpRequest",
        "x-used-method": "common.ajax",
    }


async def dashboard_save_credit_card(
    booking_id: str,
    legacy_token_id: str,
    token_card: dict[str, Any],
    *,
    property_id: str | None = None,
) -> dict:
    """Attach a credit card to a Cloudbeds reservation via the dashboard's
    internal `save_credit_card` endpoint.

    Arguments:
      booking_id: Cloudbeds INTERNAL reservation ID (the one in the
        dashboard URL hash, e.g. `175931510`). This is NOT the public
        reservation ID (e.g. `1989264686165`) — those are different and
        we need to look up internal-from-public separately.
      legacy_token_id: Stripe legacy token id (`tok_xxx`), produced by
        `stripe.createToken()` in the browser. NOT a payment method id
        (`pm_xxx`) — the dashboard's `save_credit_card` uses the legacy
        token even though the dashboard also creates a PM.
      token_card: The token's `.card` object as returned by Stripe.js,
        with keys like id, brand, exp_month, exp_year, last4, name,
        address_*, etc. We send every field including the empty ones.
      property_id: Cloudbeds property ID. Defaults to
        `settings.cloudbeds_property_id`.

    Returns:
      On success: `{"success": True, "card_id": "...", "card_details": {...},
                    "token_data": "cus_xxx|pm_yyy"}`
      On failure: `{"success": False, "error": "...", "detail": ...}`

    Pre-conditions:
      A valid Cloudbeds session must exist in the on-disk cache
      (backend/data/.cloudbeds_session.json). Bootstrap it by running
      a Playwright login script — the resulting cookies are reused for
      ~8 hours until the OAuth `at` JWT expires.
    """
    prop = property_id or settings.cloudbeds_property_id
    if not prop:
        return {"success": False, "error": "No Cloudbeds property ID configured."}

    cookies = _load_session_cookies()
    if not cookies:
        return {
            "success": False,
            "error": "No Cloudbeds session cookies on disk. "
                     "Run scripts/test_cloudbeds_login.py to bootstrap the session.",
        }

    csrf = cookies.get("csrf_accessa_cookie")
    if not csrf:
        return {
            "success": False,
            "error": "Missing csrf_accessa_cookie in cached session. "
                     "Session may be stale or login incomplete — re-bootstrap.",
        }

    form = _build_save_card_form(
        booking_id=booking_id,
        property_id=str(prop),
        legacy_token_id=legacy_token_id,
        token_card=token_card,
        csrf_accessa=csrf,
    )
    headers = _build_headers(str(prop))

    try:
        async with httpx.AsyncClient(
            cookies=cookies,
            timeout=_DEFAULT_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            resp = await client.post(_SAVE_CARD_URL, data=form, headers=headers)
    except httpx.TimeoutException:
        log.warning("dashboard_save_credit_card timed out (booking=%s)", booking_id)
        return {"success": False, "error": "Cloudbeds save_credit_card timed out."}
    except httpx.HTTPError as e:
        log.warning("dashboard_save_credit_card HTTP error: %s", e)
        return {"success": False, "error": f"HTTP error: {e}"}

    if resp.status_code != 200:
        log.warning("dashboard_save_credit_card HTTP %s: %s",
                    resp.status_code, resp.text[:500])
        return {
            "success": False,
            "error": f"HTTP {resp.status_code}",
            "detail": resp.text[:500],
        }

    try:
        body = resp.json()
    except ValueError:
        log.warning("dashboard_save_credit_card returned non-JSON: %s", resp.text[:500])
        return {
            "success": False,
            "error": "Cloudbeds returned non-JSON response",
            "detail": resp.text[:500],
        }

    if not body.get("success"):
        # Possible failure modes:
        #   - Session expired (~8h after login) — body may indicate auth failure
        #   - CSRF mismatch — typically a redirect or 403, but Cloudbeds
        #     sometimes returns 200 with success=false
        #   - Stripe-side decline propagating through (unlikely with
        #     just-tokenized card)
        #   - Validator rejected the body shape (missing/extra field)
        log.warning("dashboard_save_credit_card success=false: %s",
                    json.dumps(body)[:500])
        return {
            "success": False,
            "error": body.get("message") or body.get("error") or "Cloudbeds rejected the request.",
            "detail": body,
        }

    log.info(
        "dashboard_save_credit_card booking=%s -> card_id=%s last4=%s",
        booking_id, body.get("card_id"),
        (body.get("card_details") or {}).get("card_number"),
    )
    return {
        "success": True,
        "card_id": str(body.get("card_id", "")),
        "card_details": body.get("card_details"),
        "token_data": body.get("token_data"),
    }
