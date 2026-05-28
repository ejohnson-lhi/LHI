"""Guest portal — SMS-delivered web flows.

v1 supports the checkout button. Two audiences share this router:

  /portal/*  — DCS-facing, shared-secret auth via X-Portal-Auth header.
               DCS pushes "send the checkout SMS" requests and polls for
               guest confirmations.

  /c/{token} — guest-facing, no auth header (the token in the URL IS the
               capability). Renders a tiny HTML page; one POST confirms the
               checkout. Token is one-shot.

  /g/{id}    — guest-facing portal. `id` is either a 22-char random token
               (SMS-delivered) or a 4-digit prefix of the Cloudbeds
               reservation ID (verbally communicable). Prefix lookups are
               rate-limited and probe-detected.
"""
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.database import get_db
from app.data.room_preferences import AVAILABLE_PREFS, valid_keys
from app.models.portal_token import PortalToken
from app.tools.cloudbeds import _extract_phones_from_reservation, _get, _summarize_reservation, add_reservation_note, detect_card_type_from_pan, format_phone_display, get_reservation_by_id, normalize_phone_e164, post_credit_card, post_item, post_reservation_document, post_void_item, put_guest_contact
from app.tools.cloudbeds_dashboard import dashboard_save_credit_card, get_booking_id
from app.tools.twilio_sms import get_message_status, send_sms

log = logging.getLogger(__name__)
router = APIRouter()

TOKEN_LIFETIME = timedelta(hours=24)

# Cloudbeds' platform Stripe publishable key. Lifted from the dashboard's
# Stripe.js iframe src. Cards tokenized with THIS key produce `tok_xxx` that
# the dashboard's /hotel/save_credit_card endpoint will accept and convert
# to a chargeable card. Our own `settings.stripe_publishable_key` would
# tokenize against a DIFFERENT Stripe account and fail to attach. Public by
# design — fine to embed in HTML we serve. If Cloudbeds rotates this key
# the dashboard's iframe src is the source of truth — re-capture from a
# fresh HAR.
_CLOUDBEDS_STRIPE_PK = (
    "pk_live_51GxYvfCkb5UaC5yLKjotmnTBp7MYbmiTqeNvDluaevZJ7xSsbL7RC4f3ZQdglMa9IVY6iPkpfDCdSJGrgdiyvuRo00jZpsTHkv"
)


# ---------------------------------------------------------------------------
# Shared-secret guard for DCS-facing endpoints. Guest endpoints don't use it
# (they're token-authenticated via the URL itself).
# ---------------------------------------------------------------------------

async def require_portal_auth(x_portal_auth: Annotated[str | None, Header()] = None):
    if not settings.portal_shared_secret:
        log.warning("portal: PORTAL_SHARED_SECRET not configured — refusing DCS request")
        raise HTTPException(status_code=503, detail="portal not configured")
    if not x_portal_auth or not secrets.compare_digest(x_portal_auth, settings.portal_shared_secret):
        raise HTTPException(status_code=401, detail="bad portal auth")


# ---------------------------------------------------------------------------
# DCS → droplet: enqueue a checkout SMS
# ---------------------------------------------------------------------------

class CheckoutSmsRequest(BaseModel):
    reservation_id: str = Field(min_length=1)
    first_name: str | None = None
    phone: str = Field(min_length=1, description="E.164, e.g. +12075551234")
    room_number: str = Field(min_length=1)


class CheckoutSmsResponse(BaseModel):
    token: str
    portal_url: str
    sms_sid: str | None  # None if Twilio send failed; the token row still exists
    sms_status: str      # "sent" | "stub" | "failed"


@router.post("/portal/checkout-sms", response_model=CheckoutSmsResponse,
             dependencies=[Depends(require_portal_auth)])
async def trigger_checkout_sms(req: CheckoutSmsRequest, db: AsyncSession = Depends(get_db)):
    """Generate a one-shot token, send the checkout SMS, record the token row.

    Idempotent within the token lifetime: if there's already an unacked
    checkout token for this reservation, returns the existing token without
    sending another SMS. Lets DCS retry safely.
    """
    now = datetime.utcnow()
    existing_stmt = (
        select(PortalToken)
        .where(PortalToken.purpose == "checkout")
        .where(PortalToken.reservation_id == req.reservation_id)
        .where(PortalToken.acked_at.is_(None))
        .where(PortalToken.expires_at > now)
        .order_by(PortalToken.created_at.desc())
    )
    existing = (await db.execute(existing_stmt)).scalar_one_or_none()
    if existing is not None:
        log.info("checkout-sms: reusing existing token for res=%s (created %s)",
                 req.reservation_id, existing.created_at)
        return CheckoutSmsResponse(
            token=existing.token,
            portal_url=f"{settings.portal_public_base_url.rstrip('/')}/c/{existing.token}",
            sms_sid=None,
            sms_status="already-sent",
        )

    token = secrets.token_urlsafe(16)  # ~22 chars
    row = PortalToken(
        token=token,
        purpose="checkout",
        reservation_id=req.reservation_id,
        first_name=req.first_name,
        room_number=req.room_number,
        created_at=now,
        expires_at=now + TOKEN_LIFETIME,
    )
    db.add(row)
    await db.commit()

    portal_url = f"{settings.portal_public_base_url.rstrip('/')}/c/{token}"
    greeting = f"Hi {req.first_name}, " if req.first_name else "Hello, "
    body = (
        f"{greeting}when you're ready to leave room {req.room_number}, tap "
        f"{portal_url} so housekeeping knows. Thanks for staying with us!\n"
        f"Reply STOP to opt out."
    )

    # Test-mode safety net: redirect every SMS to ERIC_CELL_NUMBER, ignoring
    # the guest's actual phone. Logged so we can see what would have happened.
    target_phone = req.phone
    if settings.portal_test_mode:
        if not settings.eric_cell_number:
            log.warning("portal: PORTAL_TEST_MODE=true but ERIC_CELL_NUMBER is empty; refusing to send")
            return CheckoutSmsResponse(
                token=token, portal_url=portal_url, sms_sid=None,
                sms_status="test-mode-no-target",
            )
        log.info("portal: TEST_MODE redirecting SMS for res=%s (real phone ends %s) -> %s",
                 req.reservation_id, req.phone[-4:] if len(req.phone) >= 4 else "?",
                 settings.eric_cell_number)
        target_phone = settings.eric_cell_number

    result = await send_sms(target_phone, body)
    sms_sid = result.get("sid") if result.get("success") else None
    if result.get("success"):
        row.sms_sent_at = datetime.utcnow()
        row.twilio_sid = sms_sid
        await db.commit()
        status = "stub" if sms_sid == "stub" else "sent"
    else:
        log.warning("checkout-sms: Twilio send failed for res=%s: %s",
                    req.reservation_id, result.get("error"))
        status = "failed"

    return CheckoutSmsResponse(
        token=token, portal_url=portal_url, sms_sid=sms_sid, sms_status=status,
    )


# ---------------------------------------------------------------------------
# Guest-facing pages — minimal HTML, no JS framework
# ---------------------------------------------------------------------------

_BASE_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
       background: #f6f7f9; color: #222; margin: 0;
       display: flex; align-items: center; justify-content: center; min-height: 100vh;
       padding: 24px; box-sizing: border-box; }
.card { background: #fff; max-width: 480px; width: 100%;
        padding: 32px 28px; border-radius: 12px; box-shadow: 0 4px 18px rgba(0,0,0,0.08);
        text-align: center; }
h1 { font-size: 22px; margin: 0 0 12px; }
p { color: #475569; line-height: 1.5; margin: 0 0 24px; }
button { font-size: 16px; padding: 14px 24px; border-radius: 8px; cursor: pointer;
         border: 0; font-weight: 600; min-width: 140px; margin: 6px; }
.primary { background: #16a34a; color: #fff; }
.primary:hover { background: #15803d; }
.muted { background: #e2e8f0; color: #334155; }
.muted:hover { background: #cbd5e1; }
.done { color: #166534; font-weight: 600; }
.expired { color: #991b1b; font-weight: 600; }
"""


def _page(title: str, body_html: str) -> HTMLResponse:
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{title}</title>
<style>{_BASE_CSS}</style>
</head>
<body><div class="card">{body_html}</div></body>
</html>"""
    return HTMLResponse(content=html)


@router.get("/c/{token}")
async def guest_checkout_page(token: str, db: AsyncSession = Depends(get_db)):
    row = await db.get(PortalToken, token)
    if row is None:
        return _page("Link not found", "<h1>This link isn't valid.</h1><p>If you got it by SMS recently, double-check the URL or contact the front desk.</p>")
    if row.expires_at < datetime.utcnow():
        return _page("Link expired", "<h1>This link has expired.</h1><p>Please contact the front desk if you need help checking out.</p>")
    if row.confirmed_at is not None:
        return _page("Already done", f"<h1 class=\"done\">Thanks!</h1><p>We've got your checkout for room {row.room_number}. Housekeeping has been notified.</p>")
    greeting = f"Hi {row.first_name}!" if row.first_name else "Hi!"
    body = f"""
    <h1>{greeting}</h1>
    <p>Are you ready to check out of <strong>room {row.room_number}</strong>?
       Tapping the green button lets housekeeping know they can start.</p>
    <form method="post" action="/c/{token}/confirm">
        <button type="submit" class="primary">Yes, I'm checking out</button>
    </form>
    <p style="margin-top:18px; font-size:13px;">Not yet? Just close this page and come back when you're ready.</p>
    """
    return _page("Checkout", body)


@router.post("/c/{token}/confirm")
async def guest_confirm_checkout(token: str, db: AsyncSession = Depends(get_db)):
    row = await db.get(PortalToken, token)
    if row is None:
        return _page("Link not found", "<h1>This link isn't valid.</h1>")
    if row.expires_at < datetime.utcnow():
        return _page("Link expired", "<h1 class=\"expired\">This link has expired.</h1>")
    if row.confirmed_at is None:
        row.confirmed_at = datetime.utcnow()
        await db.commit()
        log.info("portal: guest confirmed checkout token=%s res=%s room=%s",
                 token, row.reservation_id, row.room_number)
    return _page("Thanks!", f"<h1 class=\"done\">Thanks!</h1><p>Housekeeping has been notified for room {row.room_number}. Safe travels.</p>")


# ---------------------------------------------------------------------------
# DCS ← droplet: pending queue + ack
# ---------------------------------------------------------------------------

class PendingCheckout(BaseModel):
    token: str
    reservation_id: str
    room_number: str
    confirmed_at: datetime


class PendingCheckoutsResponse(BaseModel):
    items: list[PendingCheckout]


@router.get("/portal/pending-checkouts", response_model=PendingCheckoutsResponse,
            dependencies=[Depends(require_portal_auth)])
async def list_pending_checkouts(db: AsyncSession = Depends(get_db)):
    """Confirmations the guest has made that DCS hasn't yet acted on."""
    stmt = (
        select(PortalToken)
        .where(PortalToken.purpose == "checkout")
        .where(PortalToken.confirmed_at.is_not(None))
        .where(PortalToken.acked_at.is_(None))
        .order_by(PortalToken.confirmed_at.asc())
    )
    result = await db.execute(stmt)
    rows = result.scalars().all()
    return PendingCheckoutsResponse(items=[
        PendingCheckout(
            token=r.token,
            reservation_id=r.reservation_id,
            room_number=r.room_number,
            confirmed_at=r.confirmed_at,
        ) for r in rows
    ])


class AckRequest(BaseModel):
    token: str


@router.post("/portal/checkout-acked",
             dependencies=[Depends(require_portal_auth)])
async def ack_checkout(req: AckRequest, db: AsyncSession = Depends(get_db)):
    row = await db.get(PortalToken, req.token)
    if row is None:
        raise HTTPException(status_code=404, detail="token not found")
    if row.acked_at is None:
        row.acked_at = datetime.utcnow()
        await db.commit()
    return JSONResponse({"acked": True, "token": req.token})


# ---------------------------------------------------------------------------
# Test-only: list recent SMS attempts with live Twilio delivery status.
# ---------------------------------------------------------------------------

class RecentSmsEntry(BaseModel):
    token: str
    reservation_id: str
    first_name: str | None
    room_number: str
    created_at: datetime
    sms_sent_at: datetime | None
    twilio_sid: str | None
    confirmed_at: datetime | None
    # Live-from-Twilio fields. Populated only if twilio_sid is set; otherwise
    # left null. status mirrors Twilio's lifecycle: queued, sending, sent,
    # delivered, undelivered, failed.
    twilio_status: str | None = None
    twilio_error_code: int | None = None
    twilio_error_message: str | None = None


class RecentSmsResponse(BaseModel):
    items: list[RecentSmsEntry]


@router.get("/portal/debug/recent-sms", response_model=RecentSmsResponse,
            dependencies=[Depends(require_portal_auth)])
async def recent_sms(limit: int = 10, db: AsyncSession = Depends(get_db)):
    """Last N portal tokens, ordered newest-first, with Twilio delivery
    status fetched live for any row that has a SID. One Twilio API call per
    row, so keep `limit` modest (default 10)."""
    if limit < 1: limit = 1
    if limit > 50: limit = 50
    stmt = (
        select(PortalToken)
        .where(PortalToken.purpose == "checkout")
        .order_by(PortalToken.created_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()
    items: list[RecentSmsEntry] = []
    for r in rows:
        entry = RecentSmsEntry(
            token=r.token,
            reservation_id=r.reservation_id,
            first_name=r.first_name,
            room_number=r.room_number,
            created_at=r.created_at,
            sms_sent_at=r.sms_sent_at,
            twilio_sid=r.twilio_sid,
            confirmed_at=r.confirmed_at,
        )
        if r.twilio_sid:
            status = await get_message_status(r.twilio_sid)
            if status.get("success"):
                entry.twilio_status = status.get("status")
                entry.twilio_error_code = status.get("error_code")
                entry.twilio_error_message = status.get("error_message")
            else:
                entry.twilio_error_message = status.get("error")
        items.append(entry)
    return RecentSmsResponse(items=items)


# ---------------------------------------------------------------------------
# Test-only: wipe all checkout tokens so the scheduler can re-send.
# ---------------------------------------------------------------------------

class ResetResponse(BaseModel):
    deleted: int


@router.post("/portal/debug/reset-checkout-tokens", response_model=ResetResponse,
             dependencies=[Depends(require_portal_auth)])
async def reset_checkout_tokens(db: AsyncSession = Depends(get_db)):
    """Delete every portal_token row with purpose='checkout'. Lets test
    scripts force a fresh send without restarting the service. Acked tokens
    are also wiped so the historical audit trail starts over -- that's fine
    for testing. Don't expose this without auth."""
    stmt = select(PortalToken).where(PortalToken.purpose == "checkout")
    rows = (await db.execute(stmt)).scalars().all()
    for row in rows:
        await db.delete(row)
    await db.commit()
    log.info("portal: wiped %d checkout token(s) via debug/reset", len(rows))
    return ResetResponse(deleted=len(rows))


# ---------------------------------------------------------------------------
# DCS ← droplet: status (used by test scripts to confirm test-mode is on)
# ---------------------------------------------------------------------------

class PortalStatusResponse(BaseModel):
    test_mode: bool
    test_phone_last4: str
    public_base_url: str


@router.get("/portal/status", response_model=PortalStatusResponse,
            dependencies=[Depends(require_portal_auth)])
async def portal_status():
    last4 = ""
    if settings.portal_test_mode and settings.eric_cell_number:
        digits = "".join(c for c in settings.eric_cell_number if c.isdigit())
        last4 = digits[-4:] if len(digits) >= 4 else digits
    return PortalStatusResponse(
        test_mode=bool(settings.portal_test_mode),
        test_phone_last4=last4,
        public_base_url=settings.portal_public_base_url,
    )


# ---------------------------------------------------------------------------
# Guest portal — long-lived per-reservation token, rich /g/{token} page.
# ---------------------------------------------------------------------------

# Stay-long tokens: from issuance through 1 day past expected check-out date.
# Fallback (when dates aren't known yet) is 60 days so the link can be issued
# at booking time for stays scheduled far ahead.
GUEST_PORTAL_FALLBACK_LIFETIME = timedelta(days=60)


class IssueGuestTokenRequest(BaseModel):
    reservation_id: str = Field(min_length=1)


class IssueGuestTokenResponse(BaseModel):
    token: str
    portal_url: str
    reservation_id: str
    is_new: bool


async def issue_guest_token_row(
    db: AsyncSession,
    *,
    reservation_id: str,
) -> tuple[str, str, bool]:
    """In-process variant of issue_guest_token: get-or-create the long-lived
    guest-portal token for a reservation. Returns (token, portal_url, is_new).
    Callers that already hold an AsyncSession (e.g. the Iris voice tool) use
    this instead of HTTP-round-tripping to the /portal/issue-guest-token
    endpoint.

    Idempotent: returns the existing token if one is still valid; otherwise
    creates a new one. Same behavior whether called via this helper or via
    the public POST endpoint."""
    now = datetime.utcnow()
    existing = (await db.execute(
        select(PortalToken)
        .where(PortalToken.purpose == "guest_portal")
        .where(PortalToken.reservation_id == reservation_id)
        .where(PortalToken.expires_at > now)
        .order_by(PortalToken.created_at.desc())
    )).scalar_one_or_none()
    if existing:
        portal_url = f"{settings.portal_public_base_url.rstrip('/')}/g/{existing.token}"
        return existing.token, portal_url, False

    # Look up the reservation just to capture first_name + room_name for SMS
    # templating. The page re-fetches Cloudbeds live on each visit so anything
    # else is read fresh; we don't snapshot stay dates here. 60-day token
    # lifetime is wide enough to cover any reasonable advance booking.
    res = await get_reservation_by_id(reservation_id)
    first_name = ""
    room_name = ""
    expires_at = now + GUEST_PORTAL_FALLBACK_LIFETIME
    if res:
        guest_name = (res.get("guest_name") or "").strip()
        first_name = guest_name.split(" ")[0] if guest_name else ""
        room_name = (res.get("room_name") or "")

    token = secrets.token_urlsafe(16)
    row = PortalToken(
        token=token,
        purpose="guest_portal",
        reservation_id=reservation_id,
        first_name=first_name,
        room_number=room_name,  # storing Cloudbeds name; page does the friendly map
        created_at=now,
        expires_at=expires_at,
    )
    db.add(row)
    await db.commit()

    log.info("portal: issued guest_portal token for res=%s (expires %s)",
             reservation_id, expires_at.isoformat())
    portal_url = f"{settings.portal_public_base_url.rstrip('/')}/g/{token}"
    return token, portal_url, True


@router.post("/portal/issue-guest-token", response_model=IssueGuestTokenResponse,
             dependencies=[Depends(require_portal_auth)])
async def issue_guest_token(req: IssueGuestTokenRequest, db: AsyncSession = Depends(get_db)):
    """Get-or-create the long-lived guest-portal token for a reservation.
    See issue_guest_token_row for the in-process variant."""
    token, portal_url, is_new = await issue_guest_token_row(
        db, reservation_id=req.reservation_id,
    )
    return IssueGuestTokenResponse(
        token=token,
        portal_url=portal_url,
        reservation_id=req.reservation_id,
        is_new=is_new,
    )


# ---- Date formatting -------------------------------------------------------

def _format_date_friendly(iso_or_date: str | None) -> str:
    """Render a date as "Fri, May 30" for portal prose / kv lists.

    Three-letter month abbreviation avoids the ambiguity of all-numeric
    formats (5/6/26 reads differently in US vs EU). Weekday adds at-a-glance
    context for short-horizon stays. Year is OMITTED on purpose — portal
    pages only show dates for the current reservation, so "Fri, May 30"
    can only mean one date. If the input doesn't parse, returns the raw
    string (or empty) so the page never blows up on bad input."""
    if not iso_or_date:
        return ""
    from datetime import date as _date
    try:
        d = _date.fromisoformat(str(iso_or_date)[:10])
    except (ValueError, TypeError):
        return str(iso_or_date)
    # `%-d` strips leading zero on POSIX but Windows strftime doesn't accept
    # it. Build with `%d` then strip the leading zero ourselves -- portable
    # across both platforms.
    return d.strftime("%a, %b %d").replace(" 0", " ")


# ---- Stay-phase → human-friendly status copy --------------------------------

def _status_for_phase(phase: str, status: str, check_in: str | None, check_out: str | None) -> dict:
    """Map a (stay_phase, Cloudbeds-status) pair to (badge_text, badge_class,
    headline, body). Drives the colored status pill + paragraph at the top
    of the portal page. Returned dict keys are used directly by the template."""
    s = (status or "").lower()
    ci_friendly = _format_date_friendly(check_in) or "your arrival date"
    co_friendly = _format_date_friendly(check_out) or ""
    if s in {"canceled", "cancelled", "no_show"}:
        return {
            "badge": "canceled", "badge_class": "muted",
            "headline": "Your reservation was canceled.",
            "body": "If this was unexpected, please call the front desk at 541-997-3221.",
        }
    if phase == "future":
        return {
            "badge": "Upcoming", "badge_class": "info",
            "headline": f"We look forward to seeing you on {ci_friendly}.",
            "body": "Return any time before your stay to confirm your info, sign the rental "
                    "agreement, and add an incidentals card.",
        }
    if phase == "arriving_today":
        return {
            "badge": "Arriving today", "badge_class": "primary",
            "headline": "We're expecting you today!",
            "body": "Front desk check-in is from about 3pm till 8pm. Once you've "
                    "completed the items below, we'll text your door code so you "
                    "can go straight to your room.",
        }
    if phase == "in_house":
        return {
            "badge": "Currently staying", "badge_class": "primary",
            "headline": "You have checked in. Thank you.",
            "body": "",
        }
    if phase == "in_house_departing_tomorrow":
        return {
            "badge": "Departing tomorrow", "badge_class": "primary",
            "headline": "We hope you've enjoyed your stay so far.",
            "body": f"Check-out tomorrow ({co_friendly}) is at 11:00 AM. "
                    "If you need a later check-out, please let us know.",
        }
    if phase == "departing_today":
        return {
            "badge": "Checkout today", "badge_class": "warn",
            "headline": "Today is your checkout day.",
            "body": "Check-out is at 11:00 AM. When you're ready to leave, the checkout button below "
                    "lets housekeeping know they can start. Late checkout (after 11:00 AM) is $10 per "
                    "hour until 3:00 PM; after 3:00 PM an additional night is charged.",
        }
    if phase == "past":
        return {
            "badge": "Thanks for staying", "badge_class": "ok",
            "headline": "Thank you for staying at The Lighthouse Inn!",
            "body": "We hope you had a great time. If anything was less than perfect, please give us "
                    "a call at (541) 997-3221 -- we'd love the chance to make it right.",
        }
    # Unknown phase / missing dates
    return {
        "badge": "Reservation", "badge_class": "muted",
        "headline": "Welcome to your guest portal.",
        "body": "Sections below will unlock as we add features.",
    }


# ---- The rich /g/{token} page -----------------------------------------------

_PORTAL_PAGE_CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
       background: #f6f7f9; color: #222; margin: 0; padding: 0;
       line-height: 1.5; }
.hotel-header { position: sticky; top: 0; z-index: 10; background: #fff;
                border-bottom: 1px solid #e2e8f0;
                box-shadow: 0 1px 3px rgba(0,0,0,0.04);
                padding: 12px 16px; }
.hotel-header-inner { max-width: 540px; margin: 0 auto; }
.hotel-name { font-size: 18px; font-weight: 700; color: #0f172a; }
.hotel-name a { color: inherit; text-decoration: none; }
.hotel-name a:hover { text-decoration: underline; }
.hotel-meta { font-size: 13px; color: #475569; margin-top: 2px; }
.hotel-meta a { color: inherit; text-decoration: none; }
.hotel-meta a[href^="tel:"] { color: #1e40af; }
.hotel-meta a:hover { text-decoration: underline; }
/* Welcome box: greeting + room/door-code/wifi consolidated into one section.
   Replaces the old separate Room & door code / WiFi / Your info accordions. */
.welcome-box { padding-top: 14px; padding-bottom: 14px; }
.welcome-box h1 { margin: 0 0 12px; }
.welcome-room { margin: 4px 0 12px; }
.welcome-room .door-code-line { font-size: 15px; color: #0f172a; margin: 8px 0 0;
                                display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.welcome-room .door-code-line .code-pill { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 20px; font-weight: 700; letter-spacing: 3px;
    background: #f1f5f9; border-radius: 6px; padding: 4px 12px; color: #0f172a; user-select: all; }
.welcome-room details.room-directions { background: transparent; box-shadow: none;
    border: 1px solid #e2e8f0; border-radius: 8px; margin: 0; }
.welcome-room details.room-directions > summary { padding: 10px 14px; font-size: 15px;
    color: #0f172a; font-weight: 500; cursor: pointer; }
.welcome-room details.room-directions > summary strong { font-weight: 700; }
.welcome-room details.room-directions > summary::after { content: "\\25BE"; margin-left: auto;
    color: #94a3b8; transition: transform 0.15s ease; }
.welcome-room details.room-directions[open] > summary::after { transform: rotate(180deg); }
.welcome-room details.room-directions > summary { display: flex; align-items: center; gap: 8px; }
.welcome-room details.room-directions > summary::-webkit-details-marker { display: none; }
.welcome-room details.room-directions > summary:hover { background: #f8fafc; }
.welcome-room details.room-directions .directions-body { padding: 4px 14px 14px; color: #334155;
    font-size: 14px; }
.welcome-wifi { font-size: 14px; color: #334155; margin: 12px 0 0;
                padding-top: 12px; border-top: 1px solid #f1f5f9; }
.welcome-wifi .wifi-creds { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                            background: #f1f5f9; border-radius: 4px; padding: 2px 8px;
                            user-select: all; font-weight: 600; color: #0f172a; }
/* Tiny reservation-ID line at the bottom of the welcome box. Out of the
   way but findable if support needs the number. */
.welcome-resid { font-size: 11px; color: #94a3b8; margin: 10px 0 0;
                 padding-top: 8px; border-top: 1px solid #f8fafc; letter-spacing: 0.2px; }
.wrap { max-width: 540px; margin: 0 auto; padding: 16px; }
h1 { font-size: 22px; margin: 0 0 8px; }
.section { background: #fff; border-radius: 12px; padding: 18px 20px; margin: 12px 0;
           box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
.section h2 { font-size: 16px; margin: 0 0 10px; color: #0f172a; display: flex; align-items: center; gap: 8px; }
.section h2 .icon { font-size: 18px; }
.section p { margin: 6px 0; color: #334155; font-size: 14px; }
.kv { display: grid; grid-template-columns: 110px 1fr; gap: 4px 12px; font-size: 14px; }
.kv dt { color: #64748b; font-weight: 500; }
.kv dd { margin: 0; color: #0f172a; }
.wifi-creds { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
              background: #f1f5f9; border-radius: 6px; padding: 6px 10px;
              display: inline-block; user-select: all; }
.code-display { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                font-size: 28px; font-weight: 700; letter-spacing: 4px;
                background: #f1f5f9; border-radius: 8px; padding: 10px 16px;
                display: inline-block; color: #0f172a; user-select: all; }
.badge { display: inline-block; font-size: 12px; font-weight: 600; padding: 4px 10px;
         border-radius: 999px; letter-spacing: 0.3px; text-transform: uppercase; }
.badge.info    { background: #dbeafe; color: #1e40af; }
.badge.primary { background: #dcfce7; color: #166534; }
.badge.warn    { background: #fef3c7; color: #92400e; }
.badge.muted   { background: #e2e8f0; color: #475569; }
.badge.ok      { background: #dcfce7; color: #166534; }
.headline { font-size: 18px; font-weight: 600; color: #0f172a; margin: 12px 0 6px; }
.countdown { font-size: 14px; color: #1e40af; font-weight: 600; margin-top: 4px; }
.countdown.muted { color: #64748b; font-weight: 500; }
/* Native <details>/<summary> accordion. No JS. */
details.accord { background: #fff; border-radius: 12px; margin: 10px 0;
                 box-shadow: 0 1px 3px rgba(0,0,0,0.06); overflow: hidden; }
details.accord > summary { list-style: none; cursor: pointer; padding: 14px 20px;
                           font-weight: 600; color: #0f172a; font-size: 15px;
                           display: flex; align-items: center; gap: 10px; }
details.accord > summary::-webkit-details-marker { display: none; }
details.accord > summary .accord-title { flex: 1; }
details.accord > summary .accord-check { color: #16a34a; font-weight: 700;
                                          font-size: 16px; display: none; }
details.accord.complete > summary .accord-check { display: inline-block; }
details.accord > summary::after { content: "\\25BE"; margin-left: 4px; color: #94a3b8;
                                  transition: transform 0.15s ease; }
details.accord[open] > summary::after { transform: rotate(180deg); }
details.accord > summary:hover { background: #f8fafc; }
.accord-body { padding: 4px 20px 18px; color: #334155; font-size: 14px; }
.accord-body p { margin: 6px 0; }
.accord-body .hint { color: #64748b; font-style: italic; }
.coming-soon ul { margin: 4px 0 0; padding-left: 20px; color: #64748b; }
.coming-soon li { margin: 4px 0; }
.gated-prompt { background: #fef3c7; border-left: 3px solid #f59e0b;
                padding: 10px 14px; border-radius: 6px; margin: 6px 0;
                color: #78350f; font-size: 14px; }
.saved-banner { background: #dcfce7; border-left: 3px solid #16a34a;
                padding: 10px 14px; border-radius: 6px; margin: 0 0 12px;
                color: #14532d; font-size: 14px; font-weight: 500; }
form.contact label { display: block; font-size: 13px; font-weight: 500;
                     color: #475569; margin: 10px 0 4px; }
form.contact input[type="text"],
form.contact input[type="tel"],
form.contact input[type="email"] {
    width: 100%; padding: 9px 11px; font-size: 15px;
    border: 1px solid #cbd5e1; border-radius: 6px; box-sizing: border-box;
}
/* Browser-native validation: a non-empty tel/email that doesn't match its
   pattern (or type=email format) renders red. Empty optional fields stay
   :valid -- pattern only fires when there's a value. */
form.contact input[type="tel"]:invalid,
form.contact input[type="email"]:invalid,
form.contact input.server-invalid {
    border-color: #ef4444; background: #fef2f2;
}
form.contact .row { display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 8px; }
form.contact .row label { grid-column: span 1; }
form.contact .consent-block { background: #f8fafc; border-radius: 8px;
                              padding: 12px 14px; margin-top: 14px;
                              font-size: 13px; color: #334155; }
form.contact .consent-block label { display: flex; gap: 8px; align-items: flex-start;
                                    cursor: pointer; margin: 0; font-weight: 500;
                                    color: #0f172a; font-size: 14px; }
form.contact .consent-block input[type="checkbox"] { margin-top: 3px; }
form.contact .consent-fine { color: #64748b; margin-top: 6px; font-size: 12px;
                             line-height: 1.45; }
form.contact button { margin-top: 14px; width: 100%; padding: 12px;
                      background: #2563eb; color: white; border: 0;
                      border-radius: 6px; font-size: 15px; font-weight: 600;
                      cursor: pointer; }
form.contact button:hover { background: #1d4ed8; }
form.contact.pet label.pet-choice { display: flex; align-items: center;
    gap: 10px; margin: 8px 0; padding: 10px 12px; cursor: pointer;
    font-weight: 500; color: #0f172a; font-size: 14px;
    border: 1px solid #e2e8f0; border-radius: 8px; background: #fff; }
form.contact.pet label.pet-choice:hover { background: #f8fafc; }
form.contact.pet label.pet-choice em { color: #64748b; font-style: normal; }
form.contact.sign .agreement-text { font-size: 13px; color: #334155;
    background: #f8fafc; border-radius: 8px; padding: 12px 14px;
    max-height: 240px; overflow-y: auto; white-space: pre-wrap;
    border: 1px solid #e2e8f0; margin: 8px 0 14px; }
form.contact.sign .sig-wrap { position: relative; border: 1px solid #cbd5e1;
    border-radius: 8px; background: #fff; }
form.contact.sign canvas.sig-pad { display: block; width: 100%;
    height: 160px; touch-action: none; cursor: crosshair;
    border-radius: 8px; }
form.contact.sign .sig-clear { position: absolute; top: 6px; right: 6px;
    background: #fff; border: 1px solid #cbd5e1; color: #475569;
    font-size: 12px; padding: 4px 10px; border-radius: 6px;
    cursor: pointer; }
form.contact.sign .sig-clear:hover { background: #f1f5f9; }
form.contact.sign .sig-hint { font-size: 12px; color: #94a3b8;
    margin-top: 4px; }
.signed-summary { background: #f0fdf4; border-left: 3px solid #16a34a;
    border-radius: 8px; padding: 12px 14px; color: #14532d; font-size: 14px; }
.signed-summary p { margin: 4px 0; }
.signed-summary .label { color: #16a34a; font-weight: 700;
    font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px; }
.card-list { margin: 6px 0 14px; }
.card-row { display: flex; align-items: center; gap: 10px;
    border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px 14px;
    margin: 6px 0; background: #fff; font-size: 14px; }
.card-row.virtual { border-color: #fbbf24; background: #fffbeb; }
.card-brand { font-weight: 600; color: #0f172a; min-width: 70px; }
.card-last4 { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    color: #475569; letter-spacing: 1px; }
.card-tag { margin-left: auto; font-size: 12px; padding: 2px 8px;
    border-radius: 999px; font-weight: 600; }
.card-tag.ok { background: #dcfce7; color: #166534; }
.card-tag.warn { background: #fef3c7; color: #92400e; }
.card-info { font-size: 13px; color: #475569; margin: 6px 0 10px; }
#stripe-card-element { padding: 11px 12px; border: 1px solid #cbd5e1;
    border-radius: 6px; background: #fff; }
#stripe-card-error { color: #b91c1c; font-size: 13px; min-height: 18px;
    margin-top: 6px; }
.add-card-btn { display: inline-block; padding: 12px 22px;
    background: #2563eb; color: white; border-radius: 6px;
    font-size: 15px; font-weight: 600; text-decoration: none;
    margin-top: 6px; cursor: pointer; }
.add-card-btn:hover { background: #1d4ed8; }
.card-link-frame { width: 100%; height: 700px; border: 1px solid #cbd5e1;
    border-radius: 8px; margin-top: 16px; background: #fff; }
.card-link-fallback { background: #fef3c7; border-left: 3px solid #f59e0b;
    border-radius: 6px; padding: 12px 14px; margin-top: 16px;
    color: #78350f; font-size: 14px; }
.card-link-loading { padding: 40px 20px; text-align: center; color: #64748b; }
.card-link-loading .spinner {
    display: inline-block; width: 32px; height: 32px;
    border: 3px solid #e2e8f0; border-top-color: #2563eb;
    border-radius: 50%; animation: spin 0.8s linear infinite;
    margin-bottom: 10px;
}
@keyframes spin { to { transform: rotate(360deg); } }
/* Inline checkout button -- rendered in the status section on
   departing_today, not as a standalone accordion. */
.checkout-action { margin: 14px 0 6px; }
.checkout-btn { display: inline-block; padding: 12px 22px; background: #16a34a;
    color: white; border: 0; border-radius: 6px; font-size: 15px;
    font-weight: 600; cursor: pointer; }
.checkout-btn:disabled { background: #94a3b8; cursor: not-allowed; }
.checkout-btn:hover:not(:disabled) { background: #15803d; }
.checkout-action .hint { color: #64748b; font-size: 12px; margin-top: 6px; }
/* Room preferences -- two-zone drag-and-drop. Sortable.js handles the
   actual dragging; this just styles the zones + cards. */
.prefs-intro { color: #475569; font-size: 13px; margin: 4px 0 12px; }
.prefs-zones { display: grid; grid-template-columns: 1fr; gap: 14px; }
.prefs-zone { background: #f8fafc; border: 1px solid #e2e8f0;
    border-radius: 10px; padding: 12px 12px 14px; }
.prefs-zone h3 { margin: 0 0 8px; font-size: 13px; font-weight: 700;
    color: #334155; text-transform: uppercase; letter-spacing: 0.5px; }
.prefs-zone.matters h3 { color: #0f172a; }
.prefs-zone.matters { background: #ecfeff; border-color: #a5f3fc; }
.prefs-list { list-style: none; margin: 0; padding: 0; min-height: 36px; }
.prefs-list:empty::after { content: attr(data-empty); display: block;
    color: #94a3b8; font-style: italic; font-size: 13px;
    padding: 8px 6px; }
.pref-item { display: flex; align-items: center; gap: 10px;
    background: #fff; border: 1px solid #cbd5e1; border-radius: 8px;
    padding: 9px 12px; margin: 6px 0; cursor: grab;
    user-select: none; -webkit-user-select: none; touch-action: none; }
.pref-item:active { cursor: grabbing; }
.pref-handle { color: #94a3b8; font-size: 16px; line-height: 1;
    font-weight: 700; flex-shrink: 0; }
.pref-label { color: #0f172a; font-size: 14px; font-weight: 600; }
.pref-label small { display: block; color: #64748b; font-size: 12px;
    font-weight: 400; margin-top: 2px; }
.pref-priority { background: #06b6d4; color: #fff; font-size: 11px;
    font-weight: 700; min-width: 22px; height: 22px; border-radius: 999px;
    display: inline-flex; align-items: center; justify-content: center;
    margin-left: auto; flex-shrink: 0; padding: 0 6px; }
.sortable-ghost { opacity: 0.45; }
.sortable-drag { box-shadow: 0 6px 18px rgba(0,0,0,0.15); }
.prefs-form button { margin-top: 14px; width: 100%; padding: 12px;
    background: #2563eb; color: white; border: 0; border-radius: 6px;
    font-size: 15px; font-weight: 600; cursor: pointer; }
.prefs-form button:hover { background: #1d4ed8; }
.prefs-locked-note { font-size: 13px; color: #64748b; font-style: italic;
    margin: 8px 0 12px; }
.prefs-readonly-list { margin: 0; padding-left: 22px; color: #0f172a;
    font-size: 14px; }
.prefs-readonly-list li { margin: 4px 0; }
.prefs-readonly-empty { color: #94a3b8; font-style: italic; font-size: 13px; }
/* FAQ + Ask Iris -- live-matching text input, expandable matches, LLM fallback. */
.faq-intro { color: #475569; font-size: 13px; margin: 4px 0 10px; }
.faq-search { width: 100%; padding: 10px 12px; font-size: 15px;
    border: 1px solid #cbd5e1; border-radius: 8px; box-sizing: border-box;
    background: #fff; }
.faq-search:focus { outline: 2px solid #06b6d4; outline-offset: -1px; }
.faq-status { font-size: 12px; color: #64748b; margin: 4px 2px 8px;
    min-height: 16px; }
.faq-results { margin: 0; padding: 0; list-style: none; }
.faq-result { background: #fff; border: 1px solid #e2e8f0;
    border-radius: 8px; margin: 6px 0; }
.faq-result summary { padding: 10px 14px; cursor: pointer;
    font-size: 14px; font-weight: 600; color: #0f172a;
    display: flex; align-items: center; gap: 8px;
    list-style: none; }
.faq-result summary::-webkit-details-marker { display: none; }
.faq-result summary::after { content: "\\25BE"; margin-left: auto;
    color: #94a3b8; transition: transform 0.15s ease; }
.faq-result[open] summary::after { transform: rotate(180deg); }
.faq-result summary:hover { background: #f8fafc; }
.faq-answer { padding: 4px 14px 14px; color: #334155; font-size: 14px;
    line-height: 1.5; white-space: pre-wrap; }
.faq-no-match { color: #64748b; font-size: 13px; margin: 8px 2px;
    font-style: italic; }
.faq-ask-iris-btn { display: none; padding: 11px 18px; margin-top: 6px;
    background: #06b6d4; color: white; border: 0; border-radius: 6px;
    font-size: 14px; font-weight: 600; cursor: pointer; }
.faq-ask-iris-btn:hover { background: #0891b2; }
.faq-ask-iris-btn:disabled { background: #94a3b8; cursor: not-allowed; }
.faq-ask-iris-pending { color: #64748b; font-size: 13px;
    font-style: italic; margin: 8px 2px; display: none; }
.faq-iris-response { display: none; margin-top: 12px; padding: 14px 16px;
    background: #ecfeff; border: 1px solid #a5f3fc; border-radius: 10px; }
.faq-iris-response .iris-label { font-size: 11px; font-weight: 700;
    color: #0891b2; text-transform: uppercase; letter-spacing: 0.5px;
    margin-bottom: 6px; }
.faq-iris-response .iris-answer { color: #0f172a; font-size: 14px;
    line-height: 1.55; white-space: pre-wrap; }
.faq-iris-response .iris-meta { font-size: 11px; color: #64748b;
    margin-top: 8px; }
.faq-blocked { color: #92400e; background: #fef3c7;
    border: 1px solid #fcd34d; border-radius: 8px;
    padding: 10px 14px; font-size: 13px; }
.footer { text-align: center; color: #94a3b8; font-size: 12px; margin: 20px 0 8px; }
.footer a { color: #64748b; }
"""


def _hotel_header_html() -> str:
    """Pinned header with hotel name, address, phone -- shown on every portal-style page.

    Hotel name + address are clickable links that open Google Maps with a
    search for the hotel address. Phone opens the dialer. Mobile-friendly:
    the only target devices the portal sees are guests' phones.

    Google Maps search URL is generic (no API key, no hotel-specific
    place_id) so it works even if Cloudbeds renames us in their system.
    The `q=` query is URL-encoded but kept human-readable in the source."""
    from urllib.parse import quote_plus
    parts: list[str] = []
    maps_query = quote_plus(
        f"{settings.hotel_name} {settings.hotel_address}".strip()
    ) if (settings.hotel_name or settings.hotel_address) else ""
    maps_url = f"https://www.google.com/maps/search/?api=1&query={maps_query}" if maps_query else ""

    if settings.hotel_name:
        if maps_url:
            parts.append(
                f'<div class="hotel-name"><a href="{maps_url}" target="_blank" rel="noopener">{settings.hotel_name}</a></div>'
            )
        else:
            parts.append(f'<div class="hotel-name">{settings.hotel_name}</div>')
    if settings.hotel_address:
        if maps_url:
            parts.append(
                f'<div class="hotel-meta"><a href="{maps_url}" target="_blank" rel="noopener">{settings.hotel_address}</a></div>'
            )
        else:
            parts.append(f'<div class="hotel-meta">{settings.hotel_address}</div>')
    if settings.hotel_phone_display:
        tel = settings.hotel_phone_tel or settings.hotel_phone_display
        parts.append(
            f'<div class="hotel-meta"><a href="tel:{tel}">{settings.hotel_phone_display}</a></div>'
        )
    return (
        '<div class="hotel-header"><div class="hotel-header-inner">'
        + "".join(parts)
        + '</div></div>'
    )


def _portal_page(title: str, body_html: str) -> HTMLResponse:
    return HTMLResponse(content=f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{title}</title>
<style>{_PORTAL_PAGE_CSS}</style>
</head>
<body>{_hotel_header_html()}<div class="wrap">{body_html}</div></body>
</html>""")


# ---- Prefix lookup + rate limiter + bot-challenge mode for /h{prefix} -------

# In-memory sliding-window rate limiter. Per-IP buckets; reset on service
# restart. Acceptable for a single-instance droplet -- if we ever scale out
# we'll move this into the DB or Redis.
#
# Three escalating responses to suspicious behavior from one IP:
#   1. RATE_LIMIT  -- hit 429 if more than N requests/min (raw DOS protection)
#   2. CHALLENGE   -- after a few failures, require phone verification on the
#                     next prefix lookup. Doesn't block valid users; just adds
#                     a step. Cookie remembers verified guests.
#   3. BLOCK       -- after many failures, ban the IP entirely (full DOS).
#
# A frustrated human at 3-5 s page loads tops out around 4-6 attempts/min.
# A few wrong typos shouldn't lock anyone out -- the challenge step gives them
# an obvious recovery path. Outright block is reserved for clear automation.
_PREFIX_RATE_WINDOW = 60.0       # seconds
_PREFIX_RATE_LIMIT = 30          # requests/IP/window before 429 (was 8 -- too tight)
_PREFIX_CHALLENGE_THRESHOLD = 8  # fails in window -> require phone verification
_PREFIX_BLOCK_THRESHOLD = 30     # fails in window -> hard IP block
_PREFIX_BLOCK_DURATION = 900.0   # seconds (15 minutes) per-IP block
_rate_hits: dict[str, deque] = defaultdict(deque)
_rate_fails: dict[str, deque] = defaultdict(deque)
_blocked_until: dict[str, float] = {}

# Bot-attack-mode detection. Counts failed prefix lookups globally over a
# 5-minute window. When that crosses the threshold, all prefix lookups are
# forced into phone-verification mode. Tuned high enough that a small cluster
# of guests fumbling at the same time doesn't accidentally trip it.
_CHALLENGE_TRIGGER_WINDOW = 300.0   # 5 minutes
_CHALLENGE_TRIGGER_FAILS = 20       # global failed lookups to trip (was 8 -- too sensitive)
_CHALLENGE_DURATION = 600.0         # 10 minutes (was 30 -- faster recovery from false positives)
_global_fails: deque = deque()
_challenge_until: float = 0.0


def _challenge_mode_active() -> bool:
    return time.time() < _challenge_until


def _record_global_fail() -> None:
    """Track a failed prefix lookup globally; trip challenge mode if needed."""
    global _challenge_until
    now = time.time()
    while _global_fails and now - _global_fails[0] > _CHALLENGE_TRIGGER_WINDOW:
        _global_fails.popleft()
    _global_fails.append(now)
    if len(_global_fails) >= _CHALLENGE_TRIGGER_FAILS and now >= _challenge_until:
        _challenge_until = now + _CHALLENGE_DURATION
        log.warning("portal: bot-attack-mode ENABLED for %.0fs (%d failed prefix lookups in %.0fs)",
                    _CHALLENGE_DURATION, len(_global_fails), _CHALLENGE_TRIGGER_WINDOW)


# Signed cookie that proves a guest has verified for a given reservation.
# Format: "{reservation_id}.{expires_unix}.{hmac}". Validated server-side.

VERIFY_COOKIE_NAME = "portal_verify"
VERIFY_COOKIE_TTL_DAYS = 7


def _verify_signing_key() -> bytes:
    """The HMAC key for verify-cookies. Reuses portal_shared_secret, which is
    already required for any DCS<->droplet auth, so we don't add another knob."""
    key = settings.portal_shared_secret or "fallback-insecure-key"
    return key.encode("utf-8")


def _make_verify_cookie(reservation_id: str) -> str:
    exp = int(time.time()) + VERIFY_COOKIE_TTL_DAYS * 86400
    payload = f"{reservation_id}.{exp}"
    sig = hmac.new(_verify_signing_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _read_verify_cookie(raw: str | None) -> str | None:
    """Return the verified reservation_id if the cookie is valid + not expired."""
    if not raw or raw.count(".") != 2:
        return None
    res_id, exp_str, sig = raw.rsplit(".", 2)
    try:
        exp = int(exp_str)
    except ValueError:
        return None
    if time.time() > exp:
        return None
    expected = hmac.new(_verify_signing_key(),
                        f"{res_id}.{exp_str}".encode("utf-8"),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return res_id


def _attach_verify_cookie(resp: HTMLResponse, reservation_id: str) -> HTMLResponse:
    """Plant (or refresh) the verify cookie on an outgoing portal page so
    a guest who has already reached their portal once stays verified --
    even if a later bot-probe attack flips us into challenge mode.

    Knowledge of the prefix is itself a credential; we're not weakening
    the model by treating "already viewed this reservation" as proof."""
    if not reservation_id:
        return resp
    resp.set_cookie(
        key=VERIFY_COOKIE_NAME,
        value=_make_verify_cookie(reservation_id),
        max_age=VERIFY_COOKIE_TTL_DAYS * 86400,
        httponly=True, samesite="lax", secure=False,  # secure=True in prod behind HTTPS
    )
    return resp


def _client_ip(request: Request) -> str:
    """Best-effort client IP. Prefers X-Forwarded-For (Cloudflare tunnel adds it)."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _rate_check(ip: str) -> tuple[bool, str]:
    """Return (allowed, reason_if_blocked). Trims old entries as a side effect."""
    now = time.time()
    until = _blocked_until.get(ip)
    if until and now < until:
        return False, f"blocked until {datetime.fromtimestamp(until).isoformat()}"
    if until and now >= until:
        _blocked_until.pop(ip, None)
        _rate_fails[ip].clear()

    hits = _rate_hits[ip]
    while hits and now - hits[0] > _PREFIX_RATE_WINDOW:
        hits.popleft()
    if len(hits) >= _PREFIX_RATE_LIMIT:
        return False, "rate limit exceeded"
    hits.append(now)
    return True, ""


def _rate_record_failure(ip: str) -> None:
    """Track a failed prefix lookup or failed phone verification. Only the
    BLOCK threshold (much higher) triggers a hard ban. The CHALLENGE threshold
    is checked separately via _ip_requires_verify -- it doesn't bar entry,
    just inserts a verification step."""
    now = time.time()
    fails = _rate_fails[ip]
    while fails and now - fails[0] > _PREFIX_RATE_WINDOW:
        fails.popleft()
    fails.append(now)
    if len(fails) >= _PREFIX_BLOCK_THRESHOLD:
        _blocked_until[ip] = now + _PREFIX_BLOCK_DURATION
        log.warning("portal: IP %s blocked for %.0fs after %d failed prefix lookups",
                    ip, _PREFIX_BLOCK_DURATION, len(fails))


def _ip_requires_verify(ip: str) -> bool:
    """Has this IP failed enough times recently to need phone verification?
    Soft escalation -- doesn't deny access, just inserts the challenge form
    so a confused human can recover by entering their phone number."""
    now = time.time()
    fails = _rate_fails[ip]
    while fails and now - fails[0] > _PREFIX_RATE_WINDOW:
        fails.popleft()
    return len(fails) >= _PREFIX_CHALLENGE_THRESHOLD


# Cache of active reservations, refreshed at most every 30 seconds. Without
# this, valid prefixes take 3-5 s (Cloudbeds API) while invalid prefixes that
# short-circuit return in microseconds -- a timing oracle a bot could use to
# tell a malformed URL from a real-but-unknown one. With the cache, BOTH
# paths filter the same in-memory list and return at the same speed.
_reservations_cache: list[dict] = []
_reservations_cache_at: float = 0.0
_RESERVATIONS_CACHE_TTL = 30.0  # seconds


async def _get_active_reservations() -> list[dict]:
    """Return the cached list of recent+upcoming reservations. Refreshes on a
    30-second TTL. Cache is process-local; on multi-instance deployments it
    becomes per-instance (acceptable variance for this use case)."""
    global _reservations_cache, _reservations_cache_at
    now = time.time()
    if _reservations_cache and now - _reservations_cache_at < _RESERVATIONS_CACHE_TTL:
        return _reservations_cache
    if not settings.cloudbeds_property_id or not settings.cloudbeds_api_key:
        return []
    today = date.today()
    base_params = {
        "propertyID": settings.cloudbeds_property_id,
        "checkInFrom": (today - timedelta(days=60)).isoformat(),
        "checkInTo": (today + timedelta(days=120)).isoformat(),
        "checkOutFrom": (today - timedelta(days=14)).isoformat(),
        "includeGuestsDetails": "true",
        "includeAllRooms": "true",
        "includeCustomFields": "true",
        "sortByRecent": "true",
        "pageSize": "100",
    }
    fetched: list[dict] = []
    for page_num in range(1, 11):  # cap at 1000 reservations
        body = await _get("getReservations", params=dict(base_params, pageNumber=str(page_num)))
        if not body:
            break
        data = body.get("data") or []
        if not isinstance(data, list) or not data:
            break
        fetched.extend(r for r in data if isinstance(r, dict))
        if len(data) < 100:
            break
    _reservations_cache = fetched
    _reservations_cache_at = now
    log.info("portal: reservations cache refreshed (%d entries)", len(fetched))
    return fetched


async def _lookup_reservations_by_id_prefix(prefix: str) -> list[dict]:
    """Return cached reservations whose reservationID startswith prefix.
    Constant-time after the first call (no API hit), so valid and invalid
    prefixes have indistinguishable response times."""
    all_reservations = await _get_active_reservations()
    return [r for r in all_reservations
            if str(r.get("reservationID") or "").startswith(prefix)]


# ---- The /g/{identifier} dispatcher ----------------------------------------

_PORTAL_PAGE_BODY_TPL = """\
{saved_banner_html}
<div class="section welcome-box">
    <h1>Hi {guest_name_display}!</h1>
    <div class="welcome-room">
        {welcome_room_block}
    </div>
    <div class="welcome-wifi">
        Wi-Fi: <span class="wifi-creds">lighthouseinn</span>
        &nbsp;·&nbsp; <span class="wifi-creds">happyguest</span>
    </div>
    <div class="welcome-resid">Reservation {reservation_id}</div>
</div>

<div class="section">
    <span class="badge {badge_class}">{badge}</span>
    <div class="headline">{headline}</div>
    {body_html}
    <dl class="kv">
        <dt>Arriving</dt><dd>{check_in}</dd>
        <dt>Departing</dt><dd>{check_out}</dd>
    </dl>
    {countdown_html}
</div>

{ordered_sections_html}

<div class="footer">
    Need help? Call <a href="tel:{phone_tel}">{phone_display}</a>.<br/>
    Reply STOP to any of our texts to opt out.
</div>
"""


# ---- Section ordering rules -------------------------------------------------
#
# Sections move based on completion state + stay phase. The user's stated
# pattern: incomplete items + the FAQ live in "Group A" (above the FAQ
# anchor), completed items drop into "Group B" (below the FAQ). Cancel is
# always last. Check out floats to the top of Group A on the departing day.
#
# Implementation: every section gets a numeric priority; lower numbers
# render first. Group-A priorities are 1-99, Group-B are 110-199, Cancel
# is 999. Sort, join, drop into the template.

_SECTION_PRIORITY_GROUP_B_OFFSET = 100  # done items: original + 100 -> below FAQ (60)
_SECTION_PRIORITY_CHECKOUT_TODAY = 1     # bumps to top of Group A on departing_today
_SECTION_PRIORITY_CANCEL = 999            # always last
_SECTION_PRIORITY_DEFAULTS = {
    # name -> Group-A priority
    "contact":  10,
    "sign":     20,
    "card":     30,
    "pet":      40,
    "prefs":    50,
    "faq":      60,
    "checkout": 70,
}


_CHECKED_IN_PHASES = (
    "in_house", "in_house_departing_tomorrow", "departing_today", "past",
)


def _section_priority(name: str, *, complete: bool, phase: str) -> int:
    """Return the sort priority for `name` given completion state + phase.

    Group A (1-99): not-yet-done items + always-on coming-soons (faq,
    prefs-pre-stay). Group B (110-199): items the guest has finished --
    they fall below the FAQ. Cancel always 999.

    Phase-driven overrides:
      - checkout floats to the top of Group A (priority 1) on
        departing_today; otherwise stays in Group A at 70.
      - prefs drops to Group B once the guest has checked in (room is
        assigned, preferences locked). The user can still see them as
        a "this is what I asked for" view but they're out of the to-do
        zone.

    `complete` is ignored for sections that can't complete (faq, prefs,
    checkout, cancel).
    """
    if name == "cancel":
        return _SECTION_PRIORITY_CANCEL
    if name == "checkout":
        return _SECTION_PRIORITY_CHECKOUT_TODAY if phase == "departing_today" else _SECTION_PRIORITY_DEFAULTS["checkout"]
    if name == "prefs":
        base = _SECTION_PRIORITY_DEFAULTS["prefs"]
        # Drop into Group B (below FAQ) when the guest has already saved
        # prefs OR when they're checked in (preferences locked). Pre-stay
        # + not saved keeps it in the to-do zone above FAQ.
        in_group_b = complete or (phase in _CHECKED_IN_PHASES)
        return base + _SECTION_PRIORITY_GROUP_B_OFFSET if in_group_b else base
    base = _SECTION_PRIORITY_DEFAULTS.get(name, 80)
    if name == "faq":
        return base  # can't complete; always Group A
    return base + _SECTION_PRIORITY_GROUP_B_OFFSET if complete else base


# Static HTML for sections without a form -- coming-soons + cancel. These
# are full <details> blocks ready to drop into the ordered list.

# Room preferences is no longer a coming-soon stub -- its accordion is
# built dynamically in _render_portal_for_reservation via
# _render_prefs_section_html (drag-and-drop two-zone selector).

# FAQ section is no longer static -- _render_faq_section_html builds the
# live search-box / matches / Ask-Iris UI dynamically based on action URLs
# bound to the active token or prefix flow.

_COMING_SOON_CHECKOUT_HTML = """\
<details class="accord">
    <summary>🚪 Check out</summary>
    <div class="accord-body">
        <p class="hint">Coming soon — tap when you've left so housekeeping can start.</p>
    </div>
</details>"""

# Inline checkout button rendered in the status section on departing_today.
# Replaces the standalone "Check out" accordion entirely -- the button
# lives where the action makes sense ("Today is your checkout day."),
# and the accordion is hidden on every other phase. Disabled placeholder
# until we wire a real /g/{token}/checkout endpoint; the existing
# /c/{token}/confirm SMS-link flow is the obvious model for that.
_CHECKOUT_INLINE_BUTTON_HTML = """\
<div class="checkout-action">
    <button type="button" class="checkout-btn" disabled>I'm checking out</button>
    <p class="hint">Coming soon — tapping this will let housekeeping know they can start.</p>
</div>"""

_COMING_SOON_CANCEL_HTML = """\
<details class="accord">
    <summary>❌ Cancel reservation</summary>
    <div class="accord-body">
        <p class="hint">Coming soon — view the refund policy and confirm cancellation.</p>
    </div>
</details>"""


def _format_stay_countdown(phase: str, start_iso: str | None, end_iso: str | None) -> str:
    """Compact 'in N days' / 'night X of Y' / 'checking out today' line under
    the dates. Returns empty string when we have nothing useful to say.

    Kept short on purpose -- the main status paragraph carries the prose."""
    from datetime import date
    today = date.today()
    try:
        start = date.fromisoformat(str(start_iso)[:10]) if start_iso else None
        end = date.fromisoformat(str(end_iso)[:10]) if end_iso else None
    except (ValueError, TypeError):
        return ""
    if start is None or end is None:
        return ""

    if phase == "future":
        days = (start - today).days
        if days == 1:
            return "Arriving tomorrow"
        return f"Arriving in {days} days"
    if phase == "arriving_today":
        return "Arriving today"
    if phase in ("in_house", "in_house_departing_tomorrow"):
        total_nights = max(1, (end - start).days)
        night = (today - start).days + 1
        if phase == "in_house_departing_tomorrow":
            return f"Night {night} of {total_nights} — checking out tomorrow"
        return f"Night {night} of {total_nights}"
    if phase == "departing_today":
        return "Checking out today"
    return ""


# Frozen disclosure text the guest agrees to. If we ever change this wording,
# bump CONSENT_VERSION so old SmsConsent rows still prove what was actually agreed.
CONSENT_VERSION = "2026-05-21.v1"
CONSENT_TEXT = (
    "This phone can receive text messages, and I agree to receive booking-related "
    "texts from Lighthouse Inn (reservation reminders, door codes, check-out "
    "prompts). Message frequency varies. Reply STOP at any time to opt out. "
    "Reply HELP for help. Message and data rates may apply."
)


async def _contact_is_acknowledged(db: AsyncSession, reservation_id: str | None) -> bool:
    """True iff the guest has tapped Save on the contact form at least once
    via this portal for this reservation.

    Used to keep the address section in "to-do" position even when
    Cloudbeds already has the fields populated -- OTA-sourced data is
    often stale and we want the guest to actively confirm it once. See
    app/models/contact_acknowledgement.py for full rationale."""
    if not reservation_id:
        return False
    from app.models.contact_acknowledgement import ContactAcknowledgement
    row = await db.get(ContactAcknowledgement, reservation_id)
    return row is not None


async def _mark_contact_acknowledged(
    db: AsyncSession,
    reservation_id: str,
    request: Request,
) -> None:
    """UPSERT a ContactAcknowledgement row for this reservation. Called
    from the contact-save handler after a successful Cloudbeds write.
    Idempotent -- a re-save just refreshes the timestamp.

    Best-effort: a failure here doesn't roll back the contact save. The
    next save will retry. Logged so we can see if it ever silently breaks."""
    if not reservation_id:
        return
    try:
        from app.models.contact_acknowledgement import ContactAcknowledgement
        existing = await db.get(ContactAcknowledgement, reservation_id)
        now = datetime.utcnow()
        if existing is not None:
            existing.acknowledged_at = now
            existing.client_ip = _client_ip(request)
            existing.user_agent = request.headers.get("user-agent", "")[:500]
        else:
            db.add(ContactAcknowledgement(
                reservation_id=reservation_id,
                acknowledged_at=now,
                client_ip=_client_ip(request),
                user_agent=request.headers.get("user-agent", "")[:500],
            ))
        await db.commit()
    except Exception as ex:
        log.warning("portal: contact-ack upsert failed for res=%s: %s",
                    reservation_id, ex)


async def _current_sms_opt_in(db: AsyncSession, reservation_id: str | None) -> bool:
    """True if the most recent SmsConsent action on this reservation is
    opt_in. Used to drive the checkbox state in the contact form.

    Why reservation_id, not phone? The consent ROW is still stored per
    phone (TCPA requires that -- proof of consent attaches to a specific
    number we'd dial). But the FORM checkbox is asking "did you, on this
    booking, agree?" -- a reservation-scoped question. Phone-keyed lookup
    breaks when Cloudbeds returns the phone in a different field than we
    save against (e.g. "guestCellPhone='1'" legacy junk while the real
    number is in "guestPhone"). Reservation-keyed lookup is what the UX
    actually wants. Send-time gating still queries by phone -- that's a
    separate function (TBD with the actual send path)."""
    if not reservation_id:
        return False
    from app.models.sms_consent import SmsConsent  # local import: avoids circular
    from sqlalchemy import select
    stmt = (
        select(SmsConsent.action)
        .where(SmsConsent.reservation_id == reservation_id)
        .order_by(SmsConsent.id.desc())
        .limit(1)
    )
    row = (await db.execute(stmt)).first()
    return bool(row and row[0] == "opt_in")


def _esc(v: str | None) -> str:
    """Minimal HTML attribute-value escape for form pre-fill."""
    if v is None:
        return ""
    return (
        str(v)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _render_contact_block(
    res: dict,
    action_url: str,
    sms_opted_in: bool,
    overrides: dict[str, str] | None = None,
) -> str:
    """Form for editing the guest's address, phone numbers, and SMS opt-in.

    Phones are formatted for display so legacy reservations (where Cloudbeds
    has the raw "5415551234" the guest first typed) still render as
    "(541)-555-1234" without forcing a save round-trip.

    `overrides` is the post-redirect carry-back for fields the server just
    rejected -- those values pre-fill (instead of the Cloudbeds-backed value)
    AND get a `server-invalid` class so they render red even when HTML5's
    own :invalid check passes (e.g. "18087791" matches the [\\d]{7,} pattern
    but libphonenumber knows it isn't a real number)."""
    checked = "checked" if sms_opted_in else ""
    o = overrides or {}
    cell_display = o.get("cell_phone") if "cell_phone" in o else format_phone_display(res.get("guest_cell_phone"))
    phone_display = o.get("phone") if "phone" in o else format_phone_display(res.get("guest_phone"))
    cell_class = "server-invalid" if "cell_phone" in o else ""
    phone_class = "server-invalid" if "phone" in o else ""
    return f"""
<form class="contact" method="post" action="{action_url}" novalidate>
    <label for="address1">Street address</label>
    <input type="text" id="address1" name="address1" value="{_esc(res.get('guest_address'))}" autocomplete="address-line1" />

    <label for="address2">Apt / suite (optional)</label>
    <input type="text" id="address2" name="address2" value="{_esc(res.get('guest_address2'))}" autocomplete="address-line2" />

    <div class="row">
        <div>
            <label for="city">City</label>
            <input type="text" id="city" name="city" value="{_esc(res.get('guest_city'))}" autocomplete="address-level2" />
        </div>
        <div>
            <label for="state">State / Region</label>
            <input type="text" id="state" name="state" value="{_esc(res.get('guest_state'))}" autocomplete="address-level1" />
        </div>
        <div>
            <label for="zip_code">ZIP / Postal</label>
            <input type="text" id="zip_code" name="zip_code" value="{_esc(res.get('guest_zip'))}" autocomplete="postal-code" />
        </div>
    </div>

    <label for="country">Country</label>
    <input type="text" id="country" name="country" value="{_esc(res.get('guest_country') or 'US')}" autocomplete="country" />

    <label for="cell_phone">Cell phone (for texts)</label>
    <input type="tel" id="cell_phone" name="cell_phone" value="{_esc(cell_display)}"
           class="{cell_class}"
           autocomplete="tel" inputmode="tel"
           pattern="[\\+\\d\\s\\(\\)\\-\\.]{{7,}}"
           title="At least 7 digits, e.g. (541)-555-7890 or +44 7911 123 456" />

    <label for="phone">Other phone (optional)</label>
    <input type="tel" id="phone" name="phone" value="{_esc(phone_display)}"
           class="{phone_class}"
           autocomplete="tel" inputmode="tel"
           pattern="[\\+\\d\\s\\(\\)\\-\\.]{{7,}}"
           title="At least 7 digits, e.g. (541)-555-7890 or +44 7911 123 456" />

    <label for="email">Email</label>
    <input type="email" id="email" name="email" value="{_esc(res.get('guest_email'))}" autocomplete="email" />

    <div class="consent-block">
        <label for="sms_consent">
            <input type="checkbox" id="sms_consent" name="sms_consent" value="yes" {checked} />
            <span>{CONSENT_TEXT}</span>
        </label>
        <p class="consent-fine">Consent is not a condition of any purchase. Texts go to the cell phone above.</p>
    </div>

    <button type="submit">Save</button>
</form>
"""


_SAVED_BANNERS = {
    "contact": ("saved-banner", "Saved -- your contact info is updated."),
    "contact_phone_warn": (
        "gated-prompt",
        "Saved -- but a phone number didn't look valid and was not saved. "
        "Please review the highlighted field and re-save.",
    ),
    "contact_error": ("gated-prompt", "We couldn't save your contact info. Please try again, or call the front desk."),
    "pets": ("saved-banner", "Saved -- your pet declaration is updated."),
    "pets_error": ("gated-prompt", "We couldn't update the pet fee. Please try again, or call the front desk."),
    "sign": ("saved-banner", "Signed -- thank you. A PDF copy is now attached to your reservation."),
    "sign_uploaded_locally": (
        "gated-prompt",
        "Your signature is recorded, but we couldn't attach the PDF to your reservation right now. "
        "We'll retry automatically. No action needed.",
    ),
    "sign_error": ("gated-prompt", "We couldn't save your signature. Please try again, or call the front desk."),
    "card": ("saved-banner", "Card on file -- thank you. Charges run on the morning of your scheduled arrival."),
    "card_error": ("gated-prompt", "We couldn't save the card. Please try again, or call the front desk."),
    "prefs": ("saved-banner", "Saved -- room preferences updated."),
    "prefs_error": ("gated-prompt", "We couldn't save your preferences. Please try again."),
}


# Agreement constants. Bumping AGREEMENT_VERSION invalidates the read-only
# state for new signers (existing signatures still display the version they
# signed under -- the audit row is frozen). Replace AGREEMENT_TEXT with the
# canonical rental-agreement copy when ready.
AGREEMENT_VERSION = "2026-05-21.v2"
AGREEMENT_TEXT = (
    "Welcome to the Florence Lighthouse Inn. By signing below, I agree to the following:\n\n"
    "Smoking & odors. Please don't smoke in your room -- not everyone wants "
    "to get high at the same time you do! A smoking area is at the south end "
    "of the building. I agree to pay for smoking, vaping, and cannabis "
    "damages, missing items, and pet-policy penalties. I understand a fee of "
    "$150 or more will be assessed if there has been smoking, odors, cats in "
    "the room, or other damage.\n\n"
    "Pets. I won't leave my pet unattended in the room and will respect quiet hours.\n\n"
    "Towels & cleanliness. Please don't use the white towels or cloths to "
    "clean mud or dirt from shoes, pets, the floor, or your ride. Florence "
    "is sandy -- please don't put towels on the floor. Use the towel racks instead.\n\n"
    "Fees & charges. I understand a fee will be charged for arriving late, "
    "leaving a day early, staying past check-out time (11:00 AM), and any "
    "after-hours interactions, and that the final amount is charged based on "
    "inspection. Current fee amounts are available from the front desk. If "
    "room keys are not returned to the front desk, a $250 fee will be "
    "charged. A hold may be placed on the method of payment to cover "
    "incidental costs and damages.\n\n"
    "Hours & service. While we can answer the phone 24/7, we prefer that you "
    "call between 8:00 AM and 8:00 PM. Stay-over service (fresh towels, bed "
    "straightening) is available on request.\n\n"
    "Thank you for choosing the Florence Lighthouse Inn. We hope you have a "
    "pleasant stay."
)


def _is_virtual_card(res: dict) -> bool:
    """Heuristic: cards attached to OTA bookings are usually 'virtual'
    Booking.com / Expedia BCD cards -- chargeable for the room itself but
    NOT for incidentals (they're locked to the booking amount).

    Cloudbeds' getReservation cardsOnFile doesn't expose card-source data
    directly, so we proxy off the reservation source: is_direct_booking is
    False for OTA channels. Imperfect (a guest could have manually added a
    real card to an OTA booking) but catches the common case.
    """
    return not bool(res.get("is_direct_booking"))


def _has_real_card_on_file(res: dict) -> bool:
    """True iff there's at least one card we'd accept for incidentals.
    OTA-only bookings count as 'no real card' until the guest adds one."""
    cards = res.get("cards_on_file") or []
    if not cards:
        return False
    if _is_virtual_card(res):
        return False  # cards exist but we treat them as virtual
    return True


def _render_card_block(
    res: dict,
    action_url: str,
    *,
    cardholder_default_name: str = "",
) -> str:
    """Render the card-on-file list + inline Stripe Elements 'add card' form.

    The form tokenizes the PAN with Stripe.js (Cloudbeds' platform key) and
    POSTs the resulting `tok_xxx` + card metadata as JSON to `action_url`.
    The server side resolves booking_id and calls
    /hotel/save_credit_card on the Cloudbeds dashboard. PAN/CVV never
    touch our backend.

    On success the JS reloads the portal with `?saved=card` so the freshly
    attached card appears in the list and the green saved banner shows. On
    failure the JS keeps the form mounted and surfaces the error inline so
    the guest can retry without retyping every digit.

    `cardholder_default_name` is the pre-fill for the Cardholder name field
    (usually the guest's full name from the reservation). The guest can edit
    it before submitting."""
    cards = res.get("cards_on_file") or []
    is_virtual = _is_virtual_card(res)
    card_rows_html = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        brand = (card.get("cardType") or "card").title()
        last4 = card.get("cardNumber") or "????"
        if is_virtual:
            card_rows_html.append(
                f'<div class="card-row virtual">'
                f'<span class="card-brand">{_esc(brand)}</span>'
                f'<span class="card-last4">**** {_esc(last4)}</span>'
                f'<span class="card-tag warn">OTA card</span>'
                f'</div>'
            )
        else:
            card_rows_html.append(
                f'<div class="card-row">'
                f'<span class="card-brand">{_esc(brand)}</span>'
                f'<span class="card-last4">**** {_esc(last4)}</span>'
                f'<span class="card-tag ok">On file</span>'
                f'</div>'
            )
    card_list_html = (
        f'<div class="card-list">{"".join(card_rows_html)}</div>'
        if card_rows_html else ""
    )

    if is_virtual and cards:
        intro = (
            '<p class="card-info">An incidentals credit card is required '
            'for checking in. Please add it here.</p>'
        )
    elif cards:
        intro = (
            '<p class="card-info">A card is on file for incidentals. '
            'Cards are charged on the morning of your scheduled arrival. '
            'You can replace the card by adding a new one below.</p>'
        )
    else:
        intro = (
            '<p class="card-info">No card on file yet. Please add one '
            'below for incidentals. Cards are charged on the morning of '
            'your scheduled arrival.</p>'
        )

    # All JS-injected values are JSON-encoded so an unexpected character in
    # the guest's name / URL can't break out of the string literal.
    pk_js = json.dumps(_CLOUDBEDS_STRIPE_PK)
    action_url_js = json.dumps(action_url)
    # ?saved=card refreshes the page so the new card shows in the list and
    # _saved_banner_html renders the green success banner.
    reload_url = action_url.replace("/cards", "?saved=card") + "#card-section"
    reload_url_js = json.dumps(reload_url)
    cardholder_value = _esc(cardholder_default_name or "")

    # The form's #stripe-card-element div is mounted by Stripe.js once the
    # accordion is in the DOM. We don't bother lazy-mounting on accordion
    # toggle: Stripe Elements is cheap and the iframe just sits idle until
    # the user types. Mounting eagerly keeps the markup simple and means a
    # ?open=card deep link is interactive immediately.
    return f"""{card_list_html}{intro}
<form id="card-form" novalidate>
    <label for="cardholder-name" style="display:block;font-size:13px;font-weight:500;color:#475569;margin:10px 0 4px;">Cardholder name</label>
    <input type="text" id="cardholder-name" autocomplete="cc-name"
           value="{cardholder_value}" placeholder="As shown on the card"
           style="width:100%;padding:9px 11px;font-size:15px;border:1px solid #cbd5e1;border-radius:6px;box-sizing:border-box;" />
    <label style="display:block;font-size:13px;font-weight:500;color:#475569;margin:10px 0 4px;">Card details</label>
    <div id="stripe-card-element"></div>
    <div id="stripe-card-error" role="alert"></div>
    <button id="card-submit-btn" type="submit" class="add-card-btn"
            style="border:0;width:100%;margin-top:12px;">Add card to reservation</button>
</form>
<p class="card-info" style="margin-top:8px;font-size:12px;">
    Your card details go directly to our payment processor. We never see or
    store the card number ourselves.
</p>
<script src="https://js.stripe.com/v3/"></script>
<script>
(function() {{
    const PK = {pk_js};
    const ACTION_URL = {action_url_js};
    const RELOAD_URL = {reload_url_js};
    const stripe = Stripe(PK);
    const elements = stripe.elements();
    const cardElement = elements.create('card', {{
        style: {{
            base: {{
                fontFamily: '"Segoe UI", system-ui, sans-serif',
                fontSize: '15px',
                color: '#0f172a',
                '::placeholder': {{ color: '#94a3b8' }},
            }},
        }},
    }});
    cardElement.mount('#stripe-card-element');
    const errEl = document.getElementById('stripe-card-error');
    cardElement.on('change', function(ev) {{
        errEl.textContent = ev.error ? ev.error.message : '';
    }});
    const form = document.getElementById('card-form');
    const btn = document.getElementById('card-submit-btn');
    const nameInput = document.getElementById('cardholder-name');
    form.addEventListener('submit', async function(e) {{
        e.preventDefault();
        const name = (nameInput.value || '').trim();
        if (!name) {{
            errEl.textContent = 'Please enter the cardholder name.';
            return;
        }}
        btn.disabled = true;
        const oldLabel = btn.textContent;
        btn.textContent = 'Adding card...';
        errEl.textContent = '';
        try {{
            const tk = await stripe.createToken(cardElement, {{ name }});
            if (tk.error) {{
                errEl.textContent = tk.error.message || 'Could not tokenize the card.';
                return;
            }}
            const resp = await fetch(ACTION_URL, {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{
                    token_id: tk.token.id,
                    token_card: tk.token.card,
                }}),
            }});
            const data = await resp.json().catch(() => ({{}}));
            if (resp.ok && data.success) {{
                window.location.href = RELOAD_URL;
                return;
            }}
            errEl.textContent = data.error || data.detail
                || 'We could not save the card. Please try again.';
        }} catch (ex) {{
            errEl.textContent = 'Network error: ' + (ex && ex.message ? ex.message : ex);
        }} finally {{
            btn.disabled = false;
            btn.textContent = oldLabel;
        }}
    }});
}})();
</script>
"""


async def _latest_signature_agreement(db: AsyncSession, reservation_id: str):
    """Return the SignatureAgreement row for this reservation if signed, else None."""
    if not reservation_id:
        return None
    from app.models.signature_agreement import SignatureAgreement
    from sqlalchemy import select
    stmt = (
        select(SignatureAgreement)
        .where(SignatureAgreement.reservation_id == reservation_id)
        .order_by(SignatureAgreement.id.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _render_sign_block(latest_sig, action_url: str, guest_name: str) -> str:
    """Return either the read-only summary (if already signed) or the
    signing form (canvas + typed name + Sign button)."""
    if latest_sig is not None:
        signed_local = latest_sig.signed_at.replace(tzinfo=None)
        attached = (
            "<p><span class=\"label\">PDF</span> Attached to your Cloudbeds reservation.</p>"
            if latest_sig.cloudbeds_attached
            else "<p><span class=\"label\">PDF</span> Signed locally; the file will be "
                 "attached automatically when the connection retries.</p>"
        )
        return f"""
<div class="signed-summary">
    <p><span class="label">Signed</span> by {_esc(latest_sig.typed_name or latest_sig.guest_name or guest_name)}</p>
    <p><span class="label">When</span> {signed_local.strftime('%B %d, %Y at %H:%M UTC')}</p>
    {attached}
    <p style="margin-top:10px; color:#475569; font-size:12px;">
        To re-sign, please contact the front desk.
    </p>
</div>
"""
    return f"""
<form class="contact sign" method="post" action="{action_url}" novalidate>
    <div class="agreement-text">{_esc(AGREEMENT_TEXT)}</div>

    <label for="typed_name">Type your name</label>
    <input type="text" id="typed_name" name="typed_name" value="{_esc(guest_name)}"
           autocomplete="name" />

    <label>Sign below (use your finger or mouse)</label>
    <div class="sig-wrap">
        <canvas id="sigPad" class="sig-pad" width="600" height="160"></canvas>
        <button type="button" class="sig-clear" onclick="window.__sigPad_clear()">Clear</button>
    </div>
    <div class="sig-hint">Your signature stays on file as a PDF attached to your reservation.</div>

    <input type="hidden" name="signature_png" id="signaturePng" value="" />
    <button type="submit" onclick="return window.__sigPad_submit(this.form)">I agree &mdash; sign</button>
</form>
<script>
(function() {{
  const cvs = document.getElementById('sigPad'); if (!cvs) return;
  const ctx = cvs.getContext('2d');
  // Match the canvas's internal pixel buffer to the rendered size so strokes
  // don't ghost or stretch. Re-run on resize for responsive layouts.
  function fitCanvas() {{
    const r = cvs.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    cvs.width  = Math.floor(r.width  * dpr);
    cvs.height = Math.floor(r.height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.lineWidth = 2; ctx.lineCap = 'round'; ctx.strokeStyle = '#0f172a';
  }}
  fitCanvas();
  window.addEventListener('resize', fitCanvas);
  let drawing = false, dirty = false;
  function ptFromEvent(ev) {{
    const r = cvs.getBoundingClientRect();
    const p = ev.touches ? ev.touches[0] : ev;
    return {{x: p.clientX - r.left, y: p.clientY - r.top}};
  }}
  function down(ev) {{ ev.preventDefault(); drawing = true; const p = ptFromEvent(ev); ctx.beginPath(); ctx.moveTo(p.x, p.y); }}
  function move(ev) {{ if (!drawing) return; ev.preventDefault(); const p = ptFromEvent(ev); ctx.lineTo(p.x, p.y); ctx.stroke(); dirty = true; }}
  function up(ev) {{ drawing = false; }}
  cvs.addEventListener('mousedown', down);
  cvs.addEventListener('mousemove', move);
  cvs.addEventListener('mouseup', up);
  cvs.addEventListener('mouseleave', up);
  cvs.addEventListener('touchstart', down, {{passive: false}});
  cvs.addEventListener('touchmove', move, {{passive: false}});
  cvs.addEventListener('touchend', up);
  window.__sigPad_clear = function() {{
    ctx.clearRect(0, 0, cvs.width, cvs.height); dirty = false;
    document.getElementById('signaturePng').value = '';
  }};
  window.__sigPad_submit = function(form) {{
    if (!dirty) {{ alert('Please draw your signature in the box first.'); return false; }}
    const data = cvs.toDataURL('image/png');
    document.getElementById('signaturePng').value = data;
    return true;
  }};
}})();
</script>
"""


async def _latest_pet_declaration(db: AsyncSession, reservation_id: str):
    """Return the most-recent PetDeclaration row for this reservation, or None."""
    if not reservation_id:
        return None
    from app.models.pet_declaration import PetDeclaration
    from sqlalchemy import select
    stmt = (
        select(PetDeclaration)
        .where(PetDeclaration.reservation_id == reservation_id)
        .order_by(PetDeclaration.id.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _render_pet_block(latest_decl, action_url: str) -> str:
    """Minimal pet form: one checkbox for "bringing a dog" + the fee note.

    Cats aren't allowed at the Lighthouse Inn (noted once at top). For
    multi-dog stays the front desk reconciles at check-in -- guests don't
    need a count field. Submitted values map to PetDeclaration.dog_count:
      - checkbox checked: dog_count = 1
      - checkbox unchecked: form submits no value, handler defaults to 0
    The handler still understands dog_count up to 3 so historical rows
    (or a future re-introduction of a count field) keep working."""
    current = latest_decl.dog_count if latest_decl else 0
    bringing = current >= 1
    checked = "checked" if bringing else ""
    return f"""
<form class="contact pet" method="post" action="{action_url}" novalidate>
    <p class="hint">Cats are not allowed at the Lighthouse Inn.</p>
    <label class="pet-choice">
        <input type="checkbox" name="dog_count" value="1" {checked} />
        <span>Yes, I'm bringing a dog(s). <em>($20 dog fee for up to a week)</em></span>
    </label>
    <button type="submit">Save</button>
</form>
"""


# Tiered dog-fee pricing. The Cloudbeds line item is $20/unit, so:
#   1-2 dogs -> 1 unit ($20)
#   3 dogs   -> 2 units ($40)
# The folio reads "Pet fee x 1" or "Pet fee x 2" -- not the dog count.
# We attach a reservation note to fill in the gap so staff can see the
# actual count without checking the local DB.
_PET_FEE_QUANTITY = {0: 0, 1: 1, 2: 1, 3: 2}
_PET_FEE_PRICE = {0: 0, 1: 20, 2: 20, 3: 40}


# ---- Room preferences -----------------------------------------------------

# Pre-stay only: phases where the guest can still edit prefs. Anything past
# this list shows a locked / read-only view. Mirrors _CHECKED_IN_PHASES
# (defined earlier near the section-priority logic) by inversion.
_PREFS_EDITABLE_PHASES = ("future", "arriving_today")


async def _load_guest_prefs(
    db: AsyncSession, reservation_id: str | None
) -> tuple[list[str], bool]:
    """Return (prioritized_keys, saved_before) for a reservation's
    preferences. `saved_before` is True iff a row exists -- used as the
    section's "complete" indicator. Unknown keys (canonical list changed
    since save) are filtered out silently."""
    if not reservation_id:
        return [], False
    from app.models.guest_preference import GuestPreference
    row = await db.get(GuestPreference, reservation_id)
    if row is None:
        return [], False
    try:
        keys = json.loads(row.prioritized_json or "[]")
    except (ValueError, TypeError):
        keys = []
    if not isinstance(keys, list):
        keys = []
    valid = valid_keys()
    keys = [k for k in keys if isinstance(k, str) and k in valid]
    return keys, True


async def _save_guest_prefs(
    db: AsyncSession,
    reservation_id: str,
    keys: list[str],
    request: Request,
) -> None:
    """UPSERT the guest's prioritized preference list. Unknown keys are
    dropped server-side; only canonical-list members survive."""
    from app.models.guest_preference import GuestPreference
    valid = valid_keys()
    # Deduplicate while preserving order (a guest might have a stale dup
    # via two saves racing; the first occurrence wins).
    seen: set[str] = set()
    ordered: list[str] = []
    for k in keys:
        if isinstance(k, str) and k in valid and k not in seen:
            seen.add(k)
            ordered.append(k)
    payload = json.dumps(ordered)
    existing = await db.get(GuestPreference, reservation_id)
    now = datetime.utcnow()
    if existing is not None:
        existing.prioritized_json = payload
        existing.updated_at = now
        existing.client_ip = _client_ip(request)
        existing.user_agent = request.headers.get("user-agent", "")[:500]
    else:
        db.add(GuestPreference(
            reservation_id=reservation_id,
            prioritized_json=payload,
            updated_at=now,
            client_ip=_client_ip(request),
            user_agent=request.headers.get("user-agent", "")[:500],
        ))
    await db.commit()


def _render_prefs_section_html(
    *,
    action_url: str,
    prioritized: list[str],
    saved_before: bool,
    phase: str,
) -> str:
    """Render the room-preferences accordion body.

    Editable view (future / arriving_today): two drop zones managed by
    Sortable.js. "Matters to me" on top (ordered, with priority numbers);
    "No preference" below (any order). JS serializes the matters-list
    order into a hidden `prefs_json` field on submit.

    Read-only view (in_house / departing_today / past): shows the saved
    list with a note that prefs are locked once the guest checks in. If
    no prefs were ever saved, shows a softer empty-state message.

    Loads Sortable.js from CDN once per page render. Lightweight (~30KB)
    and the de facto choice for touch-friendly DnD."""
    editable = phase in _PREFS_EDITABLE_PHASES

    # --- Read-only branch ----------------------------------------------
    if not editable:
        if not prioritized:
            return (
                '<p class="prefs-readonly-empty">No preferences were '
                'recorded for this stay.</p>'
            )
        items_html = "\n".join(
            f'<li>{_esc(p.label)}</li>'
            for k in prioritized
            for p in [next((p for p in AVAILABLE_PREFS if p.key == k), None)]
            if p is not None
        )
        return (
            '<p class="prefs-locked-note">Preferences are locked once '
            "you've checked in. Here's what was on file:</p>"
            f'<ol class="prefs-readonly-list">{items_html}</ol>'
        )

    # --- Editable branch -----------------------------------------------
    # Split canonical list into matters (in saved order) + nopref (rest).
    in_matters = set(prioritized)
    matters_prefs = [
        next(p for p in AVAILABLE_PREFS if p.key == k)
        for k in prioritized
        if any(p.key == k for p in AVAILABLE_PREFS)
    ]
    nopref_prefs = [p for p in AVAILABLE_PREFS if p.key not in in_matters]

    def _item(p, with_priority_idx: int | None) -> str:
        tip = f'<small>{_esc(p.tip)}</small>' if p.tip else ''
        priority = (
            f'<span class="pref-priority" aria-label="priority {with_priority_idx}">'
            f'{with_priority_idx}</span>'
            if with_priority_idx is not None else ''
        )
        return (
            f'<li class="pref-item" data-key="{_esc(p.key)}">'
            f'<span class="pref-handle" aria-hidden="true">≡</span>'
            f'<span class="pref-label">{_esc(p.label)}{tip}</span>'
            f'{priority}'
            f'</li>'
        )

    matters_html = "\n".join(_item(p, i + 1) for i, p in enumerate(matters_prefs))
    nopref_html = "\n".join(_item(p, None) for p in nopref_prefs)
    intro = (
        "Drag the things you care about into "
        "<strong>Matters to me</strong> in priority order — top of the "
        "list is most important. Anything in <strong>No preference</strong> "
        "we treat as a don't-care. Not guaranteed, but we use this when "
        "we assign rooms on the morning of arrival."
    )
    note = (
        '<p class="prefs-locked-note">Heads up: preferences lock once '
        "you've checked in.</p>"
    )

    return f"""
<p class="prefs-intro">{intro}</p>
{note}
<form class="prefs-form" method="post" action="{action_url}" novalidate>
    <div class="prefs-zones">
        <div class="prefs-zone matters">
            <h3>Matters to me (in priority order)</h3>
            <ul id="prefs-matters" class="prefs-list" data-empty="Drag preferences here.">
                {matters_html}
            </ul>
        </div>
        <div class="prefs-zone nopref">
            <h3>No preference</h3>
            <ul id="prefs-nopref" class="prefs-list" data-empty="(nothing here)">
                {nopref_html}
            </ul>
        </div>
    </div>
    <input type="hidden" name="prefs_json" id="prefs-json-input" value="[]" />
    <button type="submit">Save preferences</button>
</form>
<script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.2/Sortable.min.js"></script>
<script>
(function() {{
    const matters = document.getElementById('prefs-matters');
    const nopref = document.getElementById('prefs-nopref');
    if (!matters || !nopref || typeof Sortable === 'undefined') return;
    function refreshPriorities() {{
        // Recompute the 1..N priority pill on each "matters" item after
        // any reorder. Removes the pill from anything in "no preference".
        Array.from(matters.children).forEach(function(li, i) {{
            let pill = li.querySelector('.pref-priority');
            if (!pill) {{
                pill = document.createElement('span');
                pill.className = 'pref-priority';
                li.appendChild(pill);
            }}
            pill.textContent = (i + 1).toString();
            pill.setAttribute('aria-label', 'priority ' + (i + 1));
        }});
        Array.from(nopref.children).forEach(function(li) {{
            const pill = li.querySelector('.pref-priority');
            if (pill) pill.remove();
        }});
    }}
    const opts = {{ group: 'prefs', animation: 150, onSort: refreshPriorities }};
    Sortable.create(matters, opts);
    Sortable.create(nopref, opts);
    refreshPriorities();
    document.querySelector('.prefs-form').addEventListener('submit', function() {{
        const keys = Array.from(matters.children)
            .map(function(li) {{ return li.dataset.key || ''; }})
            .filter(Boolean);
        document.getElementById('prefs-json-input').value = JSON.stringify(keys);
    }});
}})();
</script>
"""


def _render_faq_section_html(*, match_url: str, ask_url: str, log_tap_url: str) -> str:
    """Live-matching FAQ search + Ask-Iris fallback button.

    UX:
      1. Guest types in a text box. 300ms debounce -> XHR to `match_url`.
      2. Server returns top FAQ matches + the current throttle state.
      3. Matches render as expandable <details>; clicking shows the
         answer inline.
      4. If no matches AND throttle isn't blocked: show "Ask Iris"
         button (possibly after a delay if the guest has used >10 LLM
         calls today).
      5. Button click -> POST to `ask_url` with the question. Response
         renders below in a highlighted card.
      6. If throttle is blocked: show the blocked message, no button.

    All state is server-driven -- the client just renders what comes
    back. Keeps the rate-limit / throttle logic in one place."""
    match_url_js = json.dumps(match_url)
    ask_url_js = json.dumps(ask_url)
    log_tap_url_js = json.dumps(log_tap_url)
    return f"""
<p class="faq-intro">Ask anything about the hotel, the area, or your stay. Start typing and we'll try to find the answer below.</p>
<input type="text" id="faq-search" class="faq-search"
       placeholder="Type a question..." autocomplete="off"
       aria-label="Ask a question" />
<div id="faq-status" class="faq-status" aria-live="polite"></div>
<ul id="faq-results" class="faq-results"></ul>
<p id="faq-no-match" class="faq-no-match" style="display:none">
    No FAQ entry matched your question.
</p>
<p id="faq-ask-iris-pending" class="faq-ask-iris-pending"></p>
<button type="button" id="faq-ask-iris-btn" class="faq-ask-iris-btn">
    Ask Iris (longer answer)
</button>
<div id="faq-blocked" class="faq-blocked" style="display:none"></div>
<div id="faq-iris-response" class="faq-iris-response" role="status" aria-live="polite">
    <div class="iris-label">Iris</div>
    <div class="iris-answer"></div>
    <div class="iris-meta"></div>
</div>
<script>
(function() {{
    const MATCH_URL = {match_url_js};
    const ASK_URL = {ask_url_js};
    const LOG_TAP_URL = {log_tap_url_js};
    const input = document.getElementById('faq-search');
    const status = document.getElementById('faq-status');
    const results = document.getElementById('faq-results');
    const noMatch = document.getElementById('faq-no-match');
    const askBtn = document.getElementById('faq-ask-iris-btn');
    const askPending = document.getElementById('faq-ask-iris-pending');
    const blocked = document.getElementById('faq-blocked');
    const respBox = document.getElementById('faq-iris-response');
    const respAnswer = respBox.querySelector('.iris-answer');
    const respMeta = respBox.querySelector('.iris-meta');
    if (!input) return;

    let debounceTimer = null;
    let askButtonTimer = null;
    let currentQuestion = '';
    let currentState = null;  // last throttle state from server

    function escapeHtml(s) {{
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }}

    function clearResults() {{
        results.innerHTML = '';
        noMatch.style.display = 'none';
        askBtn.style.display = 'none';
        askPending.style.display = 'none';
        blocked.style.display = 'none';
        if (askButtonTimer) {{ clearTimeout(askButtonTimer); askButtonTimer = null; }}
    }}

    // Track which (question, slug) pairs we've already logged. Without
    // this, every accordion open/close fires the toggle event again --
    // we don't want to double-log a guest who closes and re-opens the
    // same entry.
    const tappedThisSession = new Set();

    function renderMatches(matches) {{
        results.innerHTML = matches.map(function(m) {{
            return '<li><details class="faq-result" data-slug="' + escapeHtml(m.slug) + '">'
                + '<summary>' + escapeHtml(m.question) + '</summary>'
                + '<div class="faq-answer">' + escapeHtml(m.answer) + '</div>'
                + '</details></li>';
        }}).join('');
        // Bind a single `toggle` listener per <details>. Fires every
        // open AND close, so we filter inside.
        Array.from(results.querySelectorAll('details.faq-result')).forEach(function(d) {{
            d.addEventListener('toggle', function() {{
                if (!d.open) return;
                const slug = d.getAttribute('data-slug') || '';
                if (!slug || !currentQuestion) return;
                const tapKey = currentQuestion + '|' + slug;
                if (tappedThisSession.has(tapKey)) return;
                tappedThisSession.add(tapKey);
                // Fire-and-forget. Logging failures shouldn't disrupt
                // the guest's UX; we'll lose the data point and move on.
                fetch(LOG_TAP_URL, {{
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ question: currentQuestion, slug: slug }}),
                }}).catch(function() {{ /* swallow */ }});
            }});
        }});
    }}

    function showAskIris(state) {{
        if (!currentQuestion || currentQuestion.length < 3) {{
            // Don't offer the LLM for trivially short input -- avoids
            // accidental quota burn.
            return;
        }}
        if (state.blocked) {{
            blocked.textContent = state.blocked_message || 'Daily question limit reached.';
            blocked.style.display = '';
            return;
        }}
        const delay = Math.max(0, state.delay_seconds || 0);
        if (delay > 0) {{
            askPending.textContent = 'Iris is preparing... (' + delay + 's)';
            askPending.style.display = '';
            let remaining = delay;
            askButtonTimer = setInterval(function() {{
                remaining--;
                if (remaining <= 0) {{
                    clearInterval(askButtonTimer); askButtonTimer = null;
                    askPending.style.display = 'none';
                    askBtn.style.display = '';
                }} else {{
                    askPending.textContent = 'Iris is preparing... (' + remaining + 's)';
                }}
            }}, 1000);
        }} else {{
            askBtn.style.display = '';
        }}
        const remaining_count = (state.daily_limit || 0) - (state.used_today || 0);
        if (remaining_count > 0 && remaining_count <= 5) {{
            askBtn.textContent = 'Ask Iris (longer answer) - ' + remaining_count + ' left today';
        }} else {{
            askBtn.textContent = 'Ask Iris (longer answer)';
        }}
    }}

    async function fetchMatches(q) {{
        currentQuestion = q;
        clearResults();
        if (!q || q.length < 2) {{
            status.textContent = '';
            return;
        }}
        status.textContent = 'Searching...';
        try {{
            const resp = await fetch(MATCH_URL + '?q=' + encodeURIComponent(q), {{
                method: 'GET', credentials: 'same-origin',
            }});
            const data = await resp.json().catch(function() {{ return {{}}; }});
            currentState = data.throttle || {{}};
            status.textContent = '';
            if (data.matches && data.matches.length > 0) {{
                renderMatches(data.matches);
            }} else {{
                noMatch.style.display = '';
                showAskIris(currentState);
            }}
        }} catch (ex) {{
            status.textContent = 'Search failed: ' + (ex && ex.message || ex);
        }}
    }}

    input.addEventListener('input', function() {{
        const q = input.value.trim();
        if (debounceTimer) clearTimeout(debounceTimer);
        debounceTimer = setTimeout(function() {{ fetchMatches(q); }}, 300);
    }});

    askBtn.addEventListener('click', async function() {{
        if (!currentQuestion) return;
        askBtn.disabled = true;
        askBtn.textContent = 'Asking Iris...';
        respBox.style.display = 'none';
        try {{
            const resp = await fetch(ASK_URL, {{
                method: 'POST',
                credentials: 'same-origin',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ question: currentQuestion }}),
            }});
            const data = await resp.json().catch(function() {{ return {{}}; }});
            if (data.answer) {{
                respAnswer.textContent = data.answer;
                respMeta.textContent = data.web_search_used
                    ? '(answer used a web search for current info)'
                    : '';
                respBox.style.display = '';
            }} else if (data.error) {{
                respAnswer.textContent = 'I could not reach the LLM right now. Please call the front desk at 541-997-3221.';
                respMeta.textContent = '';
                respBox.style.display = '';
            }}
            if (data.throttle) {{
                currentState = data.throttle;
                if (currentState.blocked) {{
                    askBtn.style.display = 'none';
                    blocked.textContent = currentState.blocked_message || '';
                    blocked.style.display = '';
                }}
            }}
        }} catch (ex) {{
            respAnswer.textContent = 'Network error: ' + (ex && ex.message || ex);
            respMeta.textContent = '';
            respBox.style.display = '';
        }} finally {{
            askBtn.disabled = false;
            askBtn.textContent = 'Ask Iris (longer answer)';
        }}
    }});
}})();
</script>
"""


def _is_contact_section_complete(res: dict) -> bool:
    """The address+phone accordion is 'done' when we have enough info to
    reach the guest by mail AND by text. Required fields: street, city,
    state, zip, and at least one phone that PASSES validation -- a junk
    value like "1" left over from old data shouldn't earn a green check."""
    def _has(key: str) -> bool:
        return bool((res.get(key) or "").strip())
    def _phone_valid(key: str) -> bool:
        return normalize_phone_e164(res.get(key)) is not None
    return (
        _has("guest_address")
        and _has("guest_city")
        and _has("guest_state")
        and _has("guest_zip")
        and (_phone_valid("guest_cell_phone") or _phone_valid("guest_phone"))
    )


def _saved_banner_html(saved: str | None) -> str:
    if not saved:
        return ""
    entry = _SAVED_BANNERS.get(saved)
    if not entry:
        return ""
    css_class, msg = entry
    return f'<div class="{css_class}">{msg}</div>'


def _render_room_block(res: dict, signature_complete: bool, card_on_file: bool) -> str:
    """Content for the welcome box's room/door-code area.

    Three states the welcome box can render:
      (a) Gated -- guest still has paperwork. Show what's missing instead
          of room/code. The required accordions auto-open below.
      (b) Unlocked + no room yet -- room assignment runs the morning of
          arrival. Reassure the guest the code will appear here.
      (c) Unlocked + room assigned -- show the room as a collapsible
          "Room: <name>" header (tapping reveals directions) plus a
          compact door-code pill on the next line.

    Stay phase carve-outs:
      - past / unknown: brief thank-you, no room block
      - future + gate open + no room yet: same "assigned that morning"
        copy used for arriving_today guests, since either is the right
        framing
    """
    phase = res.get("stay_phase") or "unknown"
    door_code = (res.get("door_code") or "").strip()
    room_name = (res.get("room_name") or "").strip()

    if phase in ("past", "unknown"):
        return '<p class="hint">Your stay has ended. Thanks for staying with us!</p>'

    gate_open = signature_complete and card_on_file
    if not gate_open:
        missing = []
        if not signature_complete:
            missing.append("sign the rental agreement")
        if not card_on_file:
            missing.append("add a credit card")
        missing_text = " and ".join(missing)
        return (
            f'<div class="gated-prompt">Please {missing_text} below. '
            f'Once both are complete, your room number and door code will appear here.</div>'
        )

    # Gate is open. Show whatever we currently have.
    if not room_name and not door_code:
        return (
            '<p>Your room will be assigned on the morning of your arrival. '
            'Your door code will appear here as soon as it\'s set up.</p>'
        )

    parts: list[str] = []
    if room_name:
        # Directions content is a placeholder until we have per-room
        # directions captured in config. The structure is in place; fill
        # in the body when ready.
        parts.append(
            f'<details class="room-directions">'
            f'<summary>Room: <strong>{_esc(room_name)}</strong></summary>'
            f'<div class="directions-body">'
            f'<p>Directions to your room will appear here. If you can\'t find it, '
            f'please call the front desk at '
            f'<a href="tel:{settings.hotel_phone_tel}">{settings.hotel_phone_display}</a>.</p>'
            f'</div></details>'
        )
    else:
        parts.append('<p>Your room is being assigned now.</p>')
    if door_code:
        parts.append(
            f'<div class="door-code-line">Door code: '
            f'<span class="code-pill">{_esc(door_code)}</span></div>'
        )
    else:
        parts.append('<p class="hint" style="margin-top:8px;">Your door code will appear here once your room is ready.</p>')
    return "".join(parts)


async def _render_portal_for_reservation(
    res: dict,
    *,
    contact_action_url: str,
    db: AsyncSession,
    first_name_fallback: str = "",
    saved: str | None = None,
    contact_overrides: dict[str, str] | None = None,
    card_msg: str | None = None,
    card_overrides: dict[str, str] | None = None,
    open_section: str | None = None,
) -> HTMLResponse:
    guest_name = (res.get("guest_name") or "").strip()
    first_name = guest_name.split(" ")[0] if guest_name else first_name_fallback
    # Greeting uses the full name if we have one ("Hi Jane Mariner!"), else
    # falls back to the first-name placeholder ("Hi Jane!" or "Hi there!").
    guest_name_display = guest_name or (first_name_fallback or "there")
    phase = res.get("stay_phase") or "unknown"
    cb_status = res.get("status") or ""
    check_in_iso = res.get("check_in") or ""
    check_out_iso = res.get("check_out") or ""
    res_id_raw = res.get("reservation_id") or ""
    reservation_id_display = res_id_raw or "—"
    s = _status_for_phase(phase, cb_status, check_in_iso, check_out_iso)

    countdown = _format_stay_countdown(phase, res.get("start_iso"), res.get("end_iso"))
    if countdown:
        cd_class = "countdown" if phase in ("future", "arriving_today") else "countdown muted"
        countdown_html = f'<div class="{cd_class}">{countdown}</div>'
    else:
        countdown_html = ""

    # Operational gate: signature_complete drives off the SignatureAgreement
    # table; card_on_file checks whether the reservation has a non-virtual
    # card. Once both are True, _render_room_block reveals the room number
    # + door code instead of the "please finish sig + CC" prompt.
    latest_sig = await _latest_signature_agreement(db, res_id_raw)
    signature_complete = latest_sig is not None
    card_on_file = _has_real_card_on_file(res)
    welcome_room_block = _render_room_block(
        res, signature_complete=signature_complete, card_on_file=card_on_file,
    )

    sms_opted_in = await _current_sms_opt_in(db, res_id_raw)
    contact_block = _render_contact_block(
        res, contact_action_url, sms_opted_in, overrides=contact_overrides,
    )
    # Contact "complete" requires BOTH Cloudbeds-fields-OK AND
    # guest-acknowledged-via-portal. OTAs pre-populate addresses that are
    # often stale; we want the guest to actively confirm at least once.
    contact_fields_complete = _is_contact_section_complete(res)
    contact_acknowledged = await _contact_is_acknowledged(db, res_id_raw)
    contact_complete = contact_fields_complete and contact_acknowledged
    contact_title = (
        "📋 Address and phone" if contact_complete
        else "📋 Confirm address &amp; phone"
    )
    actionable_phase = phase not in ("past", "unknown")
    # Auto-open the contact section when (a) we redirected here on error
    # OR (b) the guest hasn't acknowledged yet and the stay hasn't ended.
    # The latter implements the user's "first visit, please check it"
    # rule -- closes once they save.
    contact_open_attr = (
        "open"
        if (
            saved in ("contact_phone_warn", "contact_error")
            or contact_overrides
            or (actionable_phase and not contact_complete)
        )
        else ""
    )

    # Pet declaration: latest row drives the form pre-fill and the checkmark.
    # Section is "complete" once the guest has saved it at least once --
    # even answering "no" counts as an acknowledgement.
    pet_action_url = contact_action_url.replace("/contact", "/pets")
    pet_latest = await _latest_pet_declaration(db, res_id_raw)
    pet_block = _render_pet_block(pet_latest, pet_action_url)
    pet_complete = pet_latest is not None
    pet_open_attr = "open" if saved in ("pets_error",) else ""

    # Room preferences: drag-and-drop priority list. Editable pre-stay,
    # read-only once checked in. "Complete" means the guest has saved at
    # least once (even an empty list counts -- it's explicit "I don't
    # care about any of these"). See _render_prefs_section_html.
    prefs_action_url = contact_action_url.replace("/contact", "/preferences")
    prefs_keys, prefs_saved = await _load_guest_prefs(db, res_id_raw)
    prefs_block = _render_prefs_section_html(
        action_url=prefs_action_url,
        prioritized=prefs_keys,
        saved_before=prefs_saved,
        phase=phase,
    )
    prefs_open_attr = "open" if saved in ("prefs_error",) else ""

    # FAQ + Ask Iris: live-matching search box backed by knowledge_base.md
    # with an LLM fallback when no FAQ entry matches. URLs route to the
    # match endpoint (per-keystroke XHR) and the ask-iris endpoint
    # (LLM call). Both auth via the same path scope as the page itself.
    faq_match_url = contact_action_url.replace("/contact", "/faq-match")
    faq_ask_url = contact_action_url.replace("/contact", "/ask-iris")
    faq_log_tap_url = contact_action_url.replace("/contact", "/log-faq-tap")
    faq_block = _render_faq_section_html(
        match_url=faq_match_url,
        ask_url=faq_ask_url,
        log_tap_url=faq_log_tap_url,
    )

    # Signature agreement: complete = a SignatureAgreement row exists. Form
    # vs read-only summary is decided inside _render_sign_block. Title drops
    # the imperative once signed.
    sign_action_url = contact_action_url.replace("/contact", "/sign")
    sign_block = _render_sign_block(latest_sig, sign_action_url, guest_name)
    sign_title = "✍️ Rental agreement" if signature_complete else "✍️ Sign the rental agreement"
    sign_open_attr = (
        "open"
        if (saved in ("sign_error",) or (actionable_phase and not signature_complete))
        else ""
    )

    # Credit card on file: form lists existing cards + lets the guest add
    # a new one via Stripe Elements. The form's Stripe.js call tokenizes
    # against Cloudbeds' platform Stripe account; the resulting tok_xxx is
    # POSTed to action_url as JSON and the server forwards it to
    # /hotel/save_credit_card. Section is "complete" when there's a
    # non-virtual card on the reservation.
    card_action_url = contact_action_url.replace("/contact", "/cards")
    card_block = _render_card_block(
        res, card_action_url,
        cardholder_default_name=guest_name,
    )
    card_open_attr = (
        "open"
        if (
            open_section == "card"
            or saved == "card_error"
            or card_msg
            or (actionable_phase and not card_on_file)
        )
        else ""
    )

    # Build each completable section's HTML (the four with forms). The
    # static coming-soon sections come from module constants.
    def _accord(section_id: str, complete: bool, open_attr: str, title: str, body_html: str) -> str:
        complete_class = "complete" if complete else ""
        return (
            f'<details class="accord {complete_class}" id="{section_id}" {open_attr}>\n'
            f'    <summary>\n'
            f'        <span class="accord-title">{title}</span>\n'
            f'        <span class="accord-check" aria-label="completed">✓</span>\n'
            f'    </summary>\n'
            f'    <div class="accord-body">{body_html}</div>\n'
            f'</details>'
        )

    contact_html = _accord("contact-section", contact_complete, contact_open_attr, contact_title, contact_block)
    sign_html = _accord("sign-section", signature_complete, sign_open_attr, sign_title, sign_block)
    card_html = _accord("card-section", card_on_file, card_open_attr, "💳 Credit card on file", card_block)
    pet_html = _accord("pet-section", pet_complete, pet_open_attr, "🐕 Bringing a pet?", pet_block)
    prefs_html = _accord("prefs-section", prefs_saved, prefs_open_attr, "🛏️ Room preferences", prefs_block)
    faq_html = _accord("faq-section", False, "", "❓ FAQ &amp; Ask Iris", faq_block)

    # Build the ordered list. Tuples of (priority, html). Sort by priority,
    # join. Each section's priority is computed from completion + phase
    # via _section_priority above.
    # Check Out has no standalone accordion -- it's hidden until the
    # actual checkout day, at which point the action button is rendered
    # inline in the status section (see body_html_block below). This is
    # the user's stated preference: don't clutter the section list with
    # a coming-soon stub for an action that's only valid one day.
    sections: list[tuple[int, str]] = [
        (_section_priority("contact", complete=contact_complete, phase=phase), contact_html),
        (_section_priority("sign", complete=signature_complete, phase=phase), sign_html),
        (_section_priority("card", complete=card_on_file, phase=phase), card_html),
        (_section_priority("pet", complete=pet_complete, phase=phase), pet_html),
        (_section_priority("prefs", complete=prefs_saved, phase=phase), prefs_html),
        (_section_priority("faq", complete=False, phase=phase), faq_html),
        (_section_priority("cancel", complete=False, phase=phase), _COMING_SOON_CANCEL_HTML),
    ]
    sections.sort(key=lambda t: t[0])
    ordered_sections_html = "\n\n".join(html for _, html in sections)

    # Status section body. Hide the <p> entirely when there's no body
    # copy (in_house phase has just a headline now). On departing_today
    # the checkout button gets appended under the body text so the
    # action lives where the prompt does.
    body_text = s["body"] or ""
    body_parts: list[str] = []
    if body_text:
        body_parts.append(f"<p>{body_text}</p>")
    if phase == "departing_today":
        body_parts.append(_CHECKOUT_INLINE_BUTTON_HTML)
    body_html_block = "".join(body_parts)

    body = _PORTAL_PAGE_BODY_TPL.format(
        guest_name_display=guest_name_display,
        saved_banner_html=_saved_banner_html(saved),
        badge=s["badge"], badge_class=s["badge_class"],
        headline=s["headline"], body_html=body_html_block,
        check_in=_format_date_friendly(check_in_iso) or "—",
        check_out=_format_date_friendly(check_out_iso) or "—",
        countdown_html=countdown_html,
        welcome_room_block=welcome_room_block,
        ordered_sections_html=ordered_sections_html,
        reservation_id=reservation_id_display,
        phone_tel=settings.hotel_phone_tel or settings.hotel_phone_display,
        phone_display=settings.hotel_phone_display,
    )
    return _portal_page(f"{settings.hotel_name} — Your stay", body)


_NOT_FOUND_PAGE = (
    '<div class="section"><h1>We couldn\'t find that.</h1>'
    '<p>Double-check the link or call the front desk at '
    '<a href="tel:+15419973221">(541) 997-3221</a>.</p></div>'
)
_EXPIRED_PAGE = (
    '<div class="section"><h1>This link has expired.</h1>'
    '<p>Please call the front desk at (541) 997-3221 if you need help.</p></div>'
)


@router.get("/g/{token}")
async def guest_portal_by_token(
    token: str,
    request: Request,
    saved: str | None = None,
    cell: str | None = None,
    phone: str | None = None,
    card_msg: str | None = None,
    card_name: str | None = None,
    card_zip: str | None = None,
    open: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Long-token flow: the URL that's embedded in SMS. The token IS the
    capability, so no separate verification step is needed. Untouched by the
    bot-challenge logic; SMS recipients never see the phone form."""
    ip = _client_ip(request)
    row = await db.get(PortalToken, token)
    if row is None or row.purpose != "guest_portal":
        log.info("portal: token lookup MISS ip=%s token-prefix=%s", ip, token[:6])
        return _portal_page("Not found", _NOT_FOUND_PAGE)
    if row.expires_at < datetime.utcnow():
        return _portal_page("Expired", _EXPIRED_PAGE)

    res = await get_reservation_by_id(row.reservation_id)
    if res is None:
        greeting = f"Hi {row.first_name}!" if row.first_name else "Hello!"
        return _portal_page(
            "Your stay",
            f'<div class="section"><h1>{greeting}</h1>'
            '<p>We\'re having trouble pulling your reservation details right now. '
            'Please refresh in a minute, or call (541) 997-3221 for help.</p></div>'
        )
    return await _render_portal_for_reservation(
        res,
        contact_action_url=f"/g/{token}/contact",
        db=db,
        first_name_fallback=row.first_name or "",
        saved=saved,
        contact_overrides=_overrides_from_qs(cell, phone),
        card_msg=card_msg,
        card_overrides={"card_holder_name": card_name or "", "card_address_zip": card_zip or ""},
        open_section=open,
    )


# ---- Short-URL prefix flow: /h{4-digit-prefix} (no slash) -------------------

def _trim_to_first4_digits(stem: str) -> str | None:
    """Reduce a URL stem like '7952' or '7952184200254' or '7952abc' to its
    first four digits. Returns None if there aren't four digits in there."""
    digits = "".join(c for c in stem if c.isdigit())
    return digits[:4] if len(digits) >= 4 else None


async def _resolve_prefix(prefix: str, request: Request) -> tuple[dict | None, HTMLResponse | None]:
    """Look up a reservation by 4-digit prefix. Returns (summary, error_page).
    Exactly one of the two is non-None. Tracks rate-limit + global-fail counters."""
    ip = _client_ip(request)
    ok, reason = _rate_check(ip)
    if not ok:
        log.warning("portal: prefix blocked ip=%s reason=%s prefix=%s", ip, reason, prefix)
        return None, HTMLResponse(
            status_code=429,
            content="<h1>Too many requests</h1><p>Please try again in a few minutes.</p>",
        )
    try:
        matches = await _lookup_reservations_by_id_prefix(prefix)
    except Exception as ex:
        log.exception("portal: prefix lookup error ip=%s prefix=%s: %s", ip, prefix, ex)
        return None, _portal_page("Try again", _NOT_FOUND_PAGE)

    if len(matches) == 0:
        _rate_record_failure(ip)
        _record_global_fail()
        log.info("portal: prefix MISS ip=%s prefix=%s", ip, prefix)
        return None, _portal_page("Not found", _NOT_FOUND_PAGE)
    if len(matches) > 1:
        log.warning("portal: prefix AMBIGUOUS ip=%s prefix=%s matches=%d ids=%s",
                    ip, prefix, len(matches),
                    ",".join(str(r.get("reservationID")) for r in matches))
        return None, _portal_page(
            "Please contact us",
            '<div class="section"><h1>Couldn\'t identify your reservation.</h1>'
            '<p>Multiple bookings match that code. Please call the front desk at '
            '<a href="tel:+15419973221">(541) 997-3221</a> and we\'ll look it up for you.</p></div>'
        )
    summary = _summarize_reservation(matches[0])
    log.info("portal: prefix HIT ip=%s prefix=%s res=%s guest=%s",
             ip, prefix, summary.get("reservation_id"), summary.get("guest_name"))
    return summary, None


_CHALLENGE_PAGE_TPL = """\
<div class="section">
    <h2><span class="icon">🔐</span>One quick check</h2>
    <p>For your security, please enter the phone number on your reservation.
       We're seeing extra activity right now and asking everyone to verify.</p>
    <form method="post" action="/h{prefix}" style="margin-top:14px;">
        <input type="tel" name="phone" autocomplete="tel" required
               inputmode="numeric"
               placeholder="(555) 123-4567"
               style="width:100%; padding:10px 12px; font-size:16px;
                      border:1px solid #cbd5e1; border-radius:6px; box-sizing:border-box;" />
        <button type="submit"
                style="margin-top:10px; width:100%; padding:12px;
                       background:#16a34a; color:white; border:0;
                       border-radius:6px; font-size:16px; font-weight:600; cursor:pointer;">
            Verify and continue
        </button>
    </form>
    {error_html}
</div>
<div class="footer">
    Need help? Call <a href="tel:+15419975221">(541) 997-3221</a>.
</div>
"""


_NO_PHONE_PAGE = (
    '<div class="section">'
    '<h2><span class="icon">🔐</span>One quick check</h2>'
    '<p>We don\'t have a phone number on your reservation, so we can\'t verify '
    'you automatically. Please call the front desk at '
    '<a href="tel:+15419973221">(541) 997-3221</a> to add your phone number, '
    'or enter the login code from a text message we sent you (if any).</p>'
    '<form method="post" action="/h{prefix}" style="margin-top:14px;">'
    '<input type="text" name="code" placeholder="6-digit code" required '
    'inputmode="numeric" maxlength="6" style="width:100%; padding:10px 12px; font-size:16px;'
    ' border:1px solid #cbd5e1; border-radius:6px; box-sizing:border-box;" />'
    '<button type="submit" style="margin-top:10px; width:100%; padding:12px;'
    ' background:#16a34a; color:white; border:0; border-radius:6px;'
    ' font-size:16px; font-weight:600; cursor:pointer;">Continue</button>'
    '</form>'
    '</div>'
    '<div class="footer">Front desk: <a href="tel:+15419973221">(541) 997-3221</a></div>'
)


def _resolved_prefix_for_request(stem: str) -> str | None:
    """Public helper: validate/normalize the URL stem to a 4-digit prefix."""
    return _trim_to_first4_digits(stem)


@router.get("/h{stem}")
async def guest_portal_short(
    stem: str,
    request: Request,
    saved: str | None = None,
    cell: str | None = None,
    phone: str | None = None,
    card_msg: str | None = None,
    card_name: str | None = None,
    card_zip: str | None = None,
    open: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Short-URL flow with optional bot-challenge gate.

    /h7952 -> portal for reservation 7952xxxxxx (most common case)
    /h7952184200254 -> aliased to /h7952 (front desk handed full ID)
    In bot-attack mode, intercepts with a phone-verification form unless a
    valid verify-cookie is already set on this browser.
    """
    prefix = _trim_to_first4_digits(stem)
    if prefix is None:
        # Stem doesn't yield 4 digits -- almost always a probe (e.g., /h100,
        # /habc, /h<random>). Count toward the per-IP fail counter so bot
        # sweeps trip the block, and log for visibility. Returning here
        # without a real lookup is fast, which is fine since valid lookups
        # are now cached and equally fast (no timing oracle).
        ip = _client_ip(request)
        _rate_record_failure(ip)
        _record_global_fail()
        log.info("portal: stem REJECT ip=%s stem=%s", ip, stem[:20])
        return _portal_page("Not found", _NOT_FOUND_PAGE)

    # === COOKIE BYPASS ===
    # A guest who already verified gets through even if their IP is currently
    # in lockout, challenge mode is on, or the rate limit is tripped. The
    # cookie is HMAC-signed and tied to a specific reservation_id; it only
    # bypasses when the URL prefix actually maps to THAT reservation. A bot
    # who somehow obtained a cookie for one reservation can't probe others.
    #
    # Two-step lookup so the bypass is robust against any cache state:
    #   1. Cache hit -- fast path; finds the reservation among cached matches.
    #   2. Direct API fetch by ID -- fallback when the cache doesn't have
    #      this reservation (e.g., it's outside the active-window cache).
    verified_res_id = _read_verify_cookie(request.cookies.get(VERIFY_COOKIE_NAME))
    if verified_res_id and verified_res_id.startswith(prefix):
        # Step 1: cache
        try:
            cached_matches = await _lookup_reservations_by_id_prefix(prefix)
        except Exception:
            cached_matches = []
        for raw in cached_matches:
            if str(raw.get("reservationID") or "") == verified_res_id:
                log.info("portal: cookie BYPASS (cache) ip=%s prefix=%s res=%s",
                         _client_ip(request), prefix, verified_res_id)
                return _attach_verify_cookie(
                    await _render_portal_for_reservation(
                        _summarize_reservation(raw),
                        contact_action_url=f"/h{prefix}/contact",
                        db=db,
                        saved=saved,
                        contact_overrides=_overrides_from_qs(cell, phone),
                        card_msg=card_msg,
                        card_overrides={"card_holder_name": card_name or "", "card_address_zip": card_zip or ""},
                        open_section=open,
                    ),
                    verified_res_id,
                )
        # Step 2: direct fetch -- cookie is valid, reservation just isn't in
        # the prefix-cache. Don't penalize the verified guest for that.
        try:
            res = await get_reservation_by_id(verified_res_id)
        except Exception:
            res = None
        if res:
            log.info("portal: cookie BYPASS (direct) ip=%s prefix=%s res=%s",
                     _client_ip(request), prefix, verified_res_id)
            return _attach_verify_cookie(
                await _render_portal_for_reservation(
                    res,
                    contact_action_url=f"/h{prefix}/contact",
                    db=db,
                    saved=saved,
                    contact_overrides=_overrides_from_qs(cell, phone),
                    card_msg=card_msg,
                    card_overrides={"card_holder_name": card_name or "", "card_address_zip": card_zip or ""},
                    open_section=open,
                ),
                verified_res_id,
            )
        log.warning("portal: cookie valid for %s but reservation not retrievable; falling through",
                    verified_res_id)
    elif verified_res_id:
        log.info("portal: cookie present for %s but doesn't match URL prefix %s",
                 verified_res_id, prefix)

    summary, err = await _resolve_prefix(prefix, request)
    if err is not None:
        return err
    assert summary is not None

    # Challenge required if EITHER the global attack-mode is on OR this
    # specific IP has already failed enough times to be suspicious. (The
    # cookie-bypass above already handled the verified-guest case.)
    ip = _client_ip(request)
    needs_challenge = _challenge_mode_active() or _ip_requires_verify(ip)
    if needs_challenge:
        # Need to know if there's a phone on file; if not, show the
        # no-phone fallback page (different copy + a code field instead of
        # a phone field).
        raw_match_phones = []
        try:
            raw_matches = await _lookup_reservations_by_id_prefix(prefix)
            if raw_matches:
                raw_match_phones = _extract_phones_from_reservation(raw_matches[0])
        except Exception:
            pass
        if raw_match_phones:
            body = _CHALLENGE_PAGE_TPL.format(prefix=prefix, error_html="")
        else:
            body = _NO_PHONE_PAGE.format(prefix=prefix)
        return _portal_page("Verify", body)

    # No challenge active: straight through. Plant a cookie so this
    # guest stays "verified" if a later attack flips us into challenge
    # mode -- knowing the prefix is itself the credential.
    return _attach_verify_cookie(
        await _render_portal_for_reservation(
            summary,
            contact_action_url=f"/h{prefix}/contact",
            db=db,
            saved=saved,
            contact_overrides=_overrides_from_qs(cell, phone),
            card_msg=card_msg,
            card_overrides={"card_holder_name": card_name or "", "card_address_zip": card_zip or ""},
            open_section=open,
        ),
        str(summary.get("reservation_id") or ""),
    )


@router.post("/h{stem}")
async def guest_portal_short_verify(
    stem: str,
    request: Request,
    phone: str = Form(default=""),
    code: str = Form(default=""),
):
    """Handle the phone (or code) submission from the challenge form."""
    prefix = _trim_to_first4_digits(stem)
    if prefix is None:
        return _portal_page("Not found", _NOT_FOUND_PAGE)
    ip = _client_ip(request)

    # We don't go through _resolve_prefix here because verification can
    # happen even when challenge mode wasn't active (idempotent posts).
    try:
        raw_matches = await _lookup_reservations_by_id_prefix(prefix)
    except Exception as ex:
        log.exception("portal: verify lookup error ip=%s prefix=%s: %s", ip, prefix, ex)
        return _portal_page("Try again", _NOT_FOUND_PAGE)
    if len(raw_matches) != 1:
        _rate_record_failure(ip)
        log.info("portal: verify lookup MISS/AMBIGUOUS ip=%s prefix=%s n=%d",
                 ip, prefix, len(raw_matches))
        return _portal_page("Not found", _NOT_FOUND_PAGE)
    raw_res = raw_matches[0]
    summary = _summarize_reservation(raw_res)
    res_id = str(summary.get("reservation_id") or "")

    # Phone path -- forgiving matcher accepts:
    #   - Full E.164 (+15413177925)
    #   - 10-digit US (5413177925)
    #   - Hyphenated (541-317-7925, 317-7925)
    #   - Plain digits including just the last 7 (3177925)
    # First tries strict E.164 equality. Falls back to comparing
    # digit-only suffixes -- if the input's digits are a tail of any
    # stored phone, it's a match. Minimum 7 digits to avoid trivial
    # collisions.
    if phone:
        input_digits = "".join(c for c in phone if c.isdigit())
        if len(input_digits) < 7:
            body = _CHALLENGE_PAGE_TPL.format(
                prefix=prefix,
                error_html='<p style="color:#991b1b; margin-top:10px;">Please enter at least 7 digits.</p>',
            )
            return _portal_page("Verify", body)

        matched = False
        stored_raw = _extract_phones_from_reservation(raw_res)
        # Strict E.164 path: works for fully-formed inputs.
        target_e164 = normalize_phone_e164(phone)
        if target_e164:
            stored_e164 = [normalize_phone_e164(p) for p in stored_raw]
            if target_e164 in [p for p in stored_e164 if p]:
                matched = True
        # Suffix path: works for partial inputs.
        if not matched:
            for stored in stored_raw:
                stored_digits = "".join(c for c in stored if c.isdigit())
                if stored_digits.endswith(input_digits):
                    matched = True
                    break

        if matched:
            log.info("portal: verify SUCCESS phone ip=%s res=%s", ip, res_id)
            resp = RedirectResponse(url=f"/h{prefix}", status_code=303)
            resp.set_cookie(
                key=VERIFY_COOKIE_NAME,
                value=_make_verify_cookie(res_id),
                max_age=VERIFY_COOKIE_TTL_DAYS * 86400,
                httponly=True, samesite="lax", secure=False,  # secure=True in prod behind HTTPS
            )
            return resp
        # Phone didn't match
        _rate_record_failure(ip)
        _record_global_fail()
        log.warning("portal: verify FAIL phone ip=%s res=%s", ip, res_id)
        body = _CHALLENGE_PAGE_TPL.format(
            prefix=prefix,
            error_html='<p style="color:#991b1b; margin-top:10px;">That number doesn\'t match the reservation.</p>',
        )
        return _portal_page("Verify", body)

    # Code path (future: front-desk-issued SMS code). Stubbed for now.
    if code:
        log.info("portal: verify code submitted ip=%s res=%s (feature pending)", ip, res_id)
        body = _NO_PHONE_PAGE.format(prefix=prefix) + (
            '<div class="section"><p style="color:#991b1b;">Login codes aren\'t enabled yet. '
            'Please call (541) 997-3221.</p></div>'
        )
        return _portal_page("Verify", body)

    # Nothing submitted
    body = _CHALLENGE_PAGE_TPL.format(
        prefix=prefix,
        error_html='<p style="color:#991b1b; margin-top:10px;">Please enter your phone number.</p>',
    )
    return _portal_page("Verify", body)


# ---- Confirm address / phone / SMS opt-in -----------------------------------

def _invalidate_reservations_cache() -> None:
    """Force the next _get_active_reservations() call to re-fetch. We call
    this after any Cloudbeds write so the guest sees their edits immediately
    on redirect rather than waiting out the 30s TTL."""
    global _reservations_cache_at
    _reservations_cache_at = 0.0


def _overrides_from_qs(cell: str | None, phone: str | None) -> dict[str, str]:
    """Pack the ?cell=&phone= query params (if present) into a dict the
    contact-block renderer uses as form-value overrides. Empty / missing
    params produce no entry so the normal Cloudbeds-backed pre-fill wins."""
    overrides: dict[str, str] = {}
    if cell is not None and cell != "":
        overrides["cell_phone"] = cell
    if phone is not None and phone != "":
        overrides["phone"] = phone
    return overrides


def _build_card_redirect(
    base_path: str, ok: bool, err: str,
    *, card_holder_name: str = "", card_address_zip: str = "",
) -> str:
    """[DEPRECATED 2026-05-27] Build the redirect URL after a card-add POST.

    Was the success/error redirect-target builder for the old raw-PAN form
    (`_apply_card_attach` -> `postCreditCard`). The current Stripe Elements
    flow returns JSON; the JS handler does its own window.location reload
    on success and inline error rendering on failure, so no server-side
    redirect URL is built. Kept temporarily in case the old form path is
    revived for an admin-only backend tool.

    On failure we carry back the SAFE fields the guest typed (name + ZIP)
    so they don't have to re-enter them along with the rest -- and the
    Cloudbeds error message so the next page can tell the guest WHY it
    failed instead of a generic 'couldn't save'.

    PCI: PAN/CVV/expiration are NEVER included in the redirect URL.
    They're typed fresh on each retry.
    """
    from urllib.parse import urlencode
    if ok:
        return f"{base_path}?saved=card#card-section"
    params: dict[str, str] = {"saved": "card_error"}
    if err: params["card_msg"] = err
    if card_holder_name and card_holder_name.strip():
        params["card_name"] = card_holder_name.strip()
    if card_address_zip and card_address_zip.strip():
        params["card_zip"] = card_address_zip.strip()
    return f"{base_path}?{urlencode(params)}#card-section"


def _build_contact_redirect(
    base_path: str, ok: bool, err: str, rejected: dict[str, str]
) -> str:
    """Build the redirect URL after a contact-form POST. On the partial-success
    path (Cloudbeds save succeeded but one or more phone inputs were bogus)
    we carry the offending raw values back so the GET-side render can show
    them in the form -- otherwise the form would re-pre-fill from Cloudbeds
    (which still has the OLD value, since we skipped the bad fields) and
    the user has no idea what they typed wrong. Fragment opens the form
    section and scrolls the browser to it."""
    from urllib.parse import urlencode
    if not ok:
        return f"{base_path}?saved=contact_error#contact-section"
    if err == "phone_invalid":
        params = {"saved": "contact_phone_warn"}
        if "cell_phone" in rejected:
            params["cell"] = rejected["cell_phone"]
        if "phone" in rejected:
            params["phone"] = rejected["phone"]
        return f"{base_path}?{urlencode(params)}#contact-section"
    return f"{base_path}?saved=contact#contact-section"


async def _apply_contact_update(
    reservation_id: str,
    request: Request,
    db: AsyncSession,
    *,
    address1: str = "",
    address2: str = "",
    city: str = "",
    state: str = "",
    zip_code: str = "",
    country: str = "",
    phone: str = "",
    cell_phone: str = "",
    email: str = "",
    sms_consent: str = "",
) -> tuple[bool, str, dict[str, str]]:
    """Push address/phone/email to Cloudbeds and log the SMS opt-in decision.

    Returns (ok, error_message). On failure we keep the partial edits in
    the URL so the redirect-then-render shows the user the old data again
    rather than silently dropping their input; for v1 we just flash the
    error and ask them to retry."""
    from app.models.sms_consent import SmsConsent

    res = await get_reservation_by_id(reservation_id)
    if not res:
        return False, "We couldn't load your reservation. Please refresh and try again.", {}
    guest_id = res.get("guest_id") or ""
    if not guest_id:
        log.warning("portal: contact update has no guest_id for res=%s", reservation_id)
        return False, "Couldn't identify the guest record. Please call the front desk.", {}

    # Validate + normalize phones BEFORE the Cloudbeds write. Three cases per
    # field:
    #   - empty            -> "" (explicit clear -- guest emptied the field)
    #   - valid            -> formatted "(541)-555-7890" / "+44-..."
    #   - non-empty junk   -> None (SKIP this field on the Cloudbeds write;
    #                         existing value stays; flag for the user banner)
    # libphonenumber backs the validation, so "5555555555", "1", and country-
    # less garbage all skip rather than getting saved verbatim.
    def _vet(raw: str) -> tuple[str | None, bool]:
        if not raw or not raw.strip():
            return "", False  # explicit clear
        if normalize_phone_e164(raw) is None:
            return None, True  # non-empty but bogus -> skip update
        return format_phone_display(raw), False
    cell_send, cell_invalid = _vet(cell_phone)
    phone_send, phone_invalid = _vet(phone)
    any_phone_invalid = cell_invalid or phone_invalid

    cb = await put_guest_contact(
        guest_id=guest_id,
        address1=address1, address2=address2,
        city=city, state=state, zip_code=zip_code, country=country,
        phone=phone_send, cell_phone=cell_send, email=email,
    )
    if not cb.get("success"):
        log.warning("portal: put_guest_contact failed for res=%s: %s",
                    reservation_id, cb.get("error"))
        return False, cb.get("error") or "Save failed. Please try again.", {}

    # Log every toggle of the SMS opt-in box. TCPA treats consent as per-phone,
    # so if the guest has more than one valid number on the form, write a row
    # for EACH unique E.164. Change detection happens per-phone so submitting
    # without touching the box doesn't spam the table -- but a brand-new phone
    # that's never been opted-in gets its own row even if a different number
    # on the same reservation already has consent.
    #
    # No valid phones? Still record the toggle as an audit row with
    # phone_e164="" -- so a user changing their mind isn't silently dropped
    # just because their phone fields are empty/junk. That row can't be used
    # for send-time gating (it's audit-only) but it tracks the user's intent.
    new_action = "opt_in" if (sms_consent or "").lower() in ("yes", "on", "true", "1") else "opt_out"
    target_e164s: list[str] = []
    _seen: set[str] = set()
    for raw in (cell_phone, phone):
        e = normalize_phone_e164(raw)
        if e and e not in _seen:
            _seen.add(e)
            target_e164s.append(e)

    from sqlalchemy import select
    written_phones: list[str] = []
    candidates = target_e164s if target_e164s else [""]  # [""] = audit-only path
    for e164 in candidates:
        latest_stmt = (
            select(SmsConsent.action)
            .where(SmsConsent.reservation_id == reservation_id)
            .where(SmsConsent.phone_e164 == e164)
            .order_by(SmsConsent.id.desc())
            .limit(1)
        )
        latest_for_phone = (await db.execute(latest_stmt)).scalar()
        if latest_for_phone == new_action:
            continue  # already in desired state for this phone
        db.add(SmsConsent(
            reservation_id=reservation_id,
            guest_id=guest_id,
            phone_e164=e164,
            action=new_action,
            source="guest_portal",
            consent_text=CONSENT_TEXT,
            consent_version=CONSENT_VERSION,
            client_ip=_client_ip(request),
            user_agent=request.headers.get("user-agent", "")[:500],
        ))
        written_phones.append(e164)
    if written_phones:
        await db.commit()
        log.info(
            "portal: SMS %s recorded for res=%s on %d phone(s) [%s]",
            new_action, reservation_id, len(written_phones),
            ", ".join("..." + p[-4:] if p else "(none)" for p in written_phones),
        )

    # Mark the contact section as acknowledged for this reservation. This
    # is what "completes" the address section in the portal -- not the
    # Cloudbeds data being non-empty (OTA-pre-populated addresses are
    # often stale; we want the guest's active confirmation).
    await _mark_contact_acknowledged(db, reservation_id, request)

    _invalidate_reservations_cache()
    if any_phone_invalid:
        rejected: dict[str, str] = {}
        if cell_invalid: rejected["cell_phone"] = cell_phone
        if phone_invalid: rejected["phone"] = phone
        return True, "phone_invalid", rejected
    return True, "", {}


@router.post("/g/{token}/contact")
async def post_contact_by_token(
    token: str,
    request: Request,
    address1: str = Form(default=""),
    address2: str = Form(default=""),
    city: str = Form(default=""),
    state: str = Form(default=""),
    zip_code: str = Form(default=""),
    country: str = Form(default=""),
    phone: str = Form(default=""),
    cell_phone: str = Form(default=""),
    email: str = Form(default=""),
    sms_consent: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    """POST handler for the contact form on the long-token portal page."""
    row = await db.get(PortalToken, token)
    if row is None or row.purpose != "guest_portal":
        return _portal_page("Not found", _NOT_FOUND_PAGE)
    if row.expires_at < datetime.utcnow():
        return _portal_page("Expired", _EXPIRED_PAGE)
    ok, err, rejected = await _apply_contact_update(
        row.reservation_id, request, db,
        address1=address1, address2=address2, city=city, state=state,
        zip_code=zip_code, country=country, phone=phone, cell_phone=cell_phone,
        email=email, sms_consent=sms_consent,
    )
    return RedirectResponse(
        url=_build_contact_redirect(f"/g/{token}", ok, err, rejected),
        status_code=303,
    )


@router.post("/h{stem}/contact")
async def post_contact_by_prefix(
    stem: str,
    request: Request,
    address1: str = Form(default=""),
    address2: str = Form(default=""),
    city: str = Form(default=""),
    state: str = Form(default=""),
    zip_code: str = Form(default=""),
    country: str = Form(default=""),
    phone: str = Form(default=""),
    cell_phone: str = Form(default=""),
    email: str = Form(default=""),
    sms_consent: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    """POST handler for the contact form on the /h{prefix} short-URL portal.

    Auth: requires a valid verify cookie whose reservation_id matches the
    URL prefix. Without that, a stranger could POST to /h0000/contact and
    we'd happily overwrite somebody's guest record. Cookie's set when the
    guest reaches their portal (either by completing the challenge OR by
    loading the page for the first time -- the prefix knowledge is itself
    the entry credential)."""
    prefix = _trim_to_first4_digits(stem)
    if prefix is None:
        return _portal_page("Not found", _NOT_FOUND_PAGE)
    verified_res_id = _read_verify_cookie(request.cookies.get(VERIFY_COOKIE_NAME))
    if not verified_res_id or not verified_res_id.startswith(prefix):
        # No cookie or cookie doesn't match this prefix -- redirect through
        # the GET path so they either pass the challenge or auto-verify.
        log.warning("portal: contact POST rejected ip=%s prefix=%s cookie=%s",
                    _client_ip(request), prefix, "yes" if verified_res_id else "no")
        return RedirectResponse(url=f"/h{prefix}", status_code=303)
    ok, err, rejected = await _apply_contact_update(
        verified_res_id, request, db,
        address1=address1, address2=address2, city=city, state=state,
        zip_code=zip_code, country=country, phone=phone, cell_phone=cell_phone,
        email=email, sms_consent=sms_consent,
    )
    return RedirectResponse(
        url=_build_contact_redirect(f"/h{prefix}", ok, err, rejected),
        status_code=303,
    )


# ---- Pet declaration --------------------------------------------------------

async def _apply_pet_update(
    reservation_id: str,
    request: Request,
    db: AsyncSession,
    *,
    dog_count: int,
) -> tuple[bool, str]:
    """Reconcile Cloudbeds + local state to match the guest's chosen dog
    count. Returns (ok, error_message).

    State transitions ALWAYS void-then-add (rather than try to diff item
    quantities): Cloudbeds returns one soldProductID for the whole post,
    so changing from 1 -> 2 dogs means voiding the existing fee and
    posting a new one with quantity=2. Going 1 -> 0 means voiding and
    posting nothing.

    Idempotent: if the requested count matches the latest local row, no
    Cloudbeds calls are made and no new audit row is written.
    """
    from app.models.pet_declaration import PetDeclaration

    if dog_count not in _PET_FEE_QUANTITY:
        return False, "Pet count must be 0, 1, 2, or 3."

    latest = await _latest_pet_declaration(db, reservation_id)
    previous_count = latest.dog_count if latest else 0
    previous_sold_id = (latest.sold_product_id if latest else None) or None

    if latest is not None and previous_count == dog_count:
        return True, ""  # no-op, but mark caller success so banner shows nothing

    # Fee-tier optimization: if the new dog count maps to the SAME Cloudbeds
    # item-quantity as the previous (e.g. 1 -> 2 dogs: both $20, qty=1), we
    # don't need to void+repost. Just keep the existing soldProductID and
    # write a new local row capturing the count change.
    previous_quantity = _PET_FEE_QUANTITY.get(previous_count, 0)
    new_quantity = _PET_FEE_QUANTITY[dog_count]
    new_sold_id: str | None = None

    if previous_quantity == new_quantity and previous_sold_id:
        # Same fee tier with an existing fee on the folio -- keep it.
        new_sold_id = previous_sold_id
    elif previous_quantity == new_quantity and new_quantity == 0:
        # Both sides are "no fee" -- nothing to do on Cloudbeds.
        new_sold_id = None
    else:
        # Tier actually changed (or we're catching up to a missing prior).
        # Void any existing fee FIRST so we never overlap on the folio.
        if previous_sold_id:
            void = await post_void_item(reservation_id, previous_sold_id)
            if not void.get("success"):
                log.warning(
                    "portal: pet void failed for res=%s sold=%s: %s",
                    reservation_id, previous_sold_id, void.get("error"),
                )
                return False, void.get("error") or "Couldn't remove the previous pet fee."
        if new_quantity > 0:
            add = await post_item(
                reservation_id,
                settings.cloudbeds_dog_fee_item_id,
                quantity=new_quantity,
            )
            if not add.get("success"):
                log.warning(
                    "portal: pet postItem failed for res=%s dogs=%d qty=%d: %s",
                    reservation_id, dog_count, new_quantity, add.get("error"),
                )
                return False, add.get("error") or "Couldn't add the pet fee."
            new_sold_id = add.get("sold_product_id") or None

    fee_quantity = new_quantity

    db.add(PetDeclaration(
        reservation_id=reservation_id,
        dog_count=dog_count,
        sold_product_id=new_sold_id,
        client_ip=_client_ip(request),
        user_agent=request.headers.get("user-agent", "")[:500],
    ))
    await db.commit()
    log.info(
        "portal: pet declaration res=%s prev=%d new=%d qty=%d fee=$%d sold=%s",
        reservation_id, previous_count, dog_count, fee_quantity,
        _PET_FEE_PRICE[dog_count], new_sold_id or "(none)",
    )

    # Post a reservation note so front-desk staff see the actual dog count
    # alongside the fee line (the Cloudbeds line item shows "Pet fee x 1"
    # for both 1 and 2 dogs, which is ambiguous without this annotation).
    # Best-effort -- a note failure shouldn't roll back a successful fee
    # operation. Skip when the count is unchanged (already returned above)
    # or when the new state is "no pets and never declared" (no info).
    if dog_count > 0 or previous_count > 0:
        def _label(n: int) -> str:
            if n == 0: return "no dogs"
            if n in (1, 2): return "1 or 2 dogs"
            return f"{n} dogs"
        verb = "declared" if previous_count == 0 else f"updated (was {_label(previous_count)})"
        note = (
            f"Pet declaration via guest portal: {_label(dog_count)} "
            f"({verb}). Fee on folio: ${_PET_FEE_PRICE[dog_count]}."
        )
        try:
            await add_reservation_note(reservation_id, note)
        except Exception as ex:
            log.warning("portal: pet note post failed for res=%s: %s", reservation_id, ex)

    _invalidate_reservations_cache()
    return True, ""


@router.post("/g/{token}/pets")
async def post_pets_by_token(
    token: str,
    request: Request,
    dog_count: int = Form(default=0),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(PortalToken, token)
    if row is None or row.purpose != "guest_portal":
        return _portal_page("Not found", _NOT_FOUND_PAGE)
    if row.expires_at < datetime.utcnow():
        return _portal_page("Expired", _EXPIRED_PAGE)
    ok, _err = await _apply_pet_update(row.reservation_id, request, db, dog_count=dog_count)
    code = "pets" if ok else "pets_error"
    return RedirectResponse(url=f"/g/{token}?saved={code}#pet-section", status_code=303)


@router.post("/h{stem}/pets")
async def post_pets_by_prefix(
    stem: str,
    request: Request,
    dog_count: int = Form(default=0),
    db: AsyncSession = Depends(get_db),
):
    prefix = _trim_to_first4_digits(stem)
    if prefix is None:
        return _portal_page("Not found", _NOT_FOUND_PAGE)
    verified_res_id = _read_verify_cookie(request.cookies.get(VERIFY_COOKIE_NAME))
    if not verified_res_id or not verified_res_id.startswith(prefix):
        log.warning("portal: pet POST rejected ip=%s prefix=%s cookie=%s",
                    _client_ip(request), prefix, "yes" if verified_res_id else "no")
        return RedirectResponse(url=f"/h{prefix}", status_code=303)
    ok, _err = await _apply_pet_update(verified_res_id, request, db, dog_count=dog_count)
    code = "pets" if ok else "pets_error"
    return RedirectResponse(url=f"/h{prefix}?saved={code}#pet-section", status_code=303)


# ---- Room preferences -------------------------------------------------------
#
# Guest-facing POST: parse the JSON list submitted by the Sortable.js form,
# filter against the canonical preference list, save. Refuses to save once
# the guest is checked in -- preferences lock at that point (room is
# already assigned).
#
# Two near-identical handlers (/g/{token}/preferences + /h{stem}/preferences)
# share _apply_prefs_save which does the parse + lock-check + save.


def _parse_prefs_json(raw: str) -> list[str]:
    """Best-effort parse of the form's prefs_json field. Returns a list
    of strings; any other shape (dict, int, malformed JSON, non-string
    items) collapses to []. Filtering against the canonical key list
    happens later in _save_guest_prefs -- we don't have to do it here."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [s for s in parsed if isinstance(s, str)]


async def _apply_prefs_save(
    reservation_id: str,
    request: Request,
    db: AsyncSession,
    *,
    prefs_json_raw: str,
) -> tuple[bool, str]:
    """Save the guest's prefs after running the editable-phase gate.
    Returns (ok, error_message)."""
    res = await get_reservation_by_id(reservation_id)
    phase = (res or {}).get("stay_phase") or "unknown"
    if phase not in _PREFS_EDITABLE_PHASES:
        log.info(
            "portal: prefs save refused for res=%s -- phase=%s is non-editable",
            reservation_id, phase,
        )
        return False, "Preferences are locked once you've checked in."
    keys = _parse_prefs_json(prefs_json_raw)
    await _save_guest_prefs(db, reservation_id, keys, request)
    log.info("portal: prefs saved for res=%s (%d in matters)", reservation_id, len(keys))
    return True, ""


@router.post("/g/{token}/preferences")
async def post_prefs_by_token(
    token: str,
    request: Request,
    prefs_json: str = Form(default="[]"),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(PortalToken, token)
    if row is None or row.purpose != "guest_portal":
        return _portal_page("Not found", _NOT_FOUND_PAGE)
    if row.expires_at < datetime.utcnow():
        return _portal_page("Expired", _EXPIRED_PAGE)
    ok, _err = await _apply_prefs_save(
        row.reservation_id, request, db, prefs_json_raw=prefs_json,
    )
    code = "prefs" if ok else "prefs_error"
    return RedirectResponse(url=f"/g/{token}?saved={code}#prefs-section", status_code=303)


@router.post("/h{stem}/preferences")
async def post_prefs_by_prefix(
    stem: str,
    request: Request,
    prefs_json: str = Form(default="[]"),
    db: AsyncSession = Depends(get_db),
):
    prefix = _trim_to_first4_digits(stem)
    if prefix is None:
        return _portal_page("Not found", _NOT_FOUND_PAGE)
    verified_res_id = _read_verify_cookie(request.cookies.get(VERIFY_COOKIE_NAME))
    if not verified_res_id or not verified_res_id.startswith(prefix):
        log.warning("portal: prefs POST rejected ip=%s prefix=%s cookie=%s",
                    _client_ip(request), prefix, "yes" if verified_res_id else "no")
        return RedirectResponse(url=f"/h{prefix}", status_code=303)
    ok, _err = await _apply_prefs_save(
        verified_res_id, request, db, prefs_json_raw=prefs_json,
    )
    code = "prefs" if ok else "prefs_error"
    return RedirectResponse(url=f"/h{prefix}?saved={code}#prefs-section", status_code=303)


# ---- FAQ + Ask Iris -------------------------------------------------------
#
# Two endpoint pairs per portal-flow (token + prefix):
#
#   GET .../faq-match?q=<text>   -- per-keystroke matcher. Returns the top
#                                   FAQ hits + current throttle state.
#                                   No side effects, no logging.
#   POST .../ask-iris            -- LLM fallback. Body: {"question": str}.
#                                   Calls Anthropic, logs the Q&A row,
#                                   enforces the daily LLM-call cap.
#
# Both endpoints share helpers in app.services.faq_* -- the heavy lifting
# (KB load, matching, throttle, LLM call) lives there; these routes just
# auth + wire up.


class _FaqMatchItem(BaseModel):
    slug: str
    question: str
    answer: str
    score: float


class _ThrottlePayload(BaseModel):
    used_today: int
    daily_limit: int
    blocked: bool
    delay_seconds: int
    blocked_message: str


class FaqMatchResponse(BaseModel):
    matches: list[_FaqMatchItem]
    throttle: _ThrottlePayload


class AskIrisRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


class AskIrisResponse(BaseModel):
    answer: str
    web_search_used: bool
    error: str | None
    throttle: _ThrottlePayload


async def _faq_match_payload(
    db: AsyncSession, reservation_id: str, query: str,
) -> FaqMatchResponse:
    """Shared body of the two faq-match endpoints. Resolves throttle state
    + ranks FAQ matches; returns a flat response the JS handler renders
    against."""
    from app.services.faq_match import rank_matches  # noqa: PLC0415
    from app.services.faq_throttle import get_throttle_state  # noqa: PLC0415
    ranked = rank_matches(query)
    state = await get_throttle_state(db, reservation_id)
    matches = [
        _FaqMatchItem(
            slug=entry.slug,
            question=entry.question,
            answer=entry.answer,
            score=score,
        )
        for entry, score in ranked
    ]
    return FaqMatchResponse(
        matches=matches,
        throttle=_ThrottlePayload(
            used_today=state.used_today,
            daily_limit=state.daily_limit,
            blocked=state.blocked,
            delay_seconds=state.delay_seconds,
            blocked_message=state.blocked_message,
        ),
    )


def _day_of_stay(check_in_iso: str | None, now: datetime) -> int | None:
    """Compute days-into-stay (or pre-stay) for a guest_qa row. -2 = two
    days before check-in, 0 = arrival day, 1+ = nights in. Returns None
    if we can't parse the check-in date."""
    if not check_in_iso:
        return None
    try:
        from datetime import date as _date
        ci = _date.fromisoformat(str(check_in_iso)[:10])
        return (now.date() - ci).days
    except (ValueError, TypeError):
        return None


async def _ask_iris_and_log(
    db: AsyncSession,
    reservation_id: str,
    question: str,
    request: Request,
) -> AskIrisResponse:
    """Shared body of the two ask-iris endpoints. Re-checks the throttle
    (defending against a race where the client called ask faster than the
    button-delay UX should have allowed), runs the LLM call, writes the
    guest_qa row, and returns the response payload."""
    from app.services.faq_llm import ask_iris  # noqa: PLC0415
    from app.services.faq_match import rank_matches  # noqa: PLC0415
    from app.services.faq_throttle import get_throttle_state  # noqa: PLC0415
    from app.models.guest_qa import GuestQa  # noqa: PLC0415

    pre_state = await get_throttle_state(db, reservation_id)
    if pre_state.blocked:
        return AskIrisResponse(
            answer="",
            web_search_used=False,
            error=pre_state.blocked_message,
            throttle=_ThrottlePayload(
                used_today=pre_state.used_today,
                daily_limit=pre_state.daily_limit,
                blocked=True,
                delay_seconds=pre_state.delay_seconds,
                blocked_message=pre_state.blocked_message,
            ),
        )

    # Run the LLM call. Re-rank locally so we can log which (if any) FAQ
    # entries matched -- helps staff later distinguish "guest ignored a
    # perfect FAQ match" from "no FAQ match existed."
    ranked = rank_matches(question)
    matched_slugs = [e.slug for e, _ in ranked[:5]]
    llm_result = await ask_iris(question)

    # Log the row regardless of error -- audit + visibility matter.
    res = await get_reservation_by_id(reservation_id)
    check_in = (res or {}).get("check_in") or ""
    now = datetime.utcnow()
    db.add(GuestQa(
        reservation_id=reservation_id,
        asked_at=now,
        question_text=question[:1000],
        matched_faq_slugs_json=json.dumps(matched_slugs),
        llm_used=True,
        llm_response_text=llm_result.get("answer") or None,
        llm_input_tokens=llm_result.get("input_tokens"),
        llm_output_tokens=llm_result.get("output_tokens"),
        day_of_stay=_day_of_stay(check_in, now),
        client_ip=_client_ip(request),
        user_agent=request.headers.get("user-agent", "")[:500],
    ))
    await db.commit()

    post_state = await get_throttle_state(db, reservation_id)
    return AskIrisResponse(
        answer=llm_result.get("answer") or "",
        web_search_used=bool(llm_result.get("web_search_used")),
        error=llm_result.get("error"),
        throttle=_ThrottlePayload(
            used_today=post_state.used_today,
            daily_limit=post_state.daily_limit,
            blocked=post_state.blocked,
            delay_seconds=post_state.delay_seconds,
            blocked_message=post_state.blocked_message,
        ),
    )


@router.get("/g/{token}/faq-match", response_model=FaqMatchResponse)
async def faq_match_by_token(
    token: str,
    q: str = "",
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(PortalToken, token)
    if row is None or row.purpose != "guest_portal":
        raise HTTPException(status_code=404, detail="Link not found.")
    if row.expires_at < datetime.utcnow():
        raise HTTPException(status_code=403, detail="This link has expired.")
    return await _faq_match_payload(db, row.reservation_id, q)


@router.post("/g/{token}/ask-iris", response_model=AskIrisResponse)
async def ask_iris_by_token(
    token: str,
    body: AskIrisRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(PortalToken, token)
    if row is None or row.purpose != "guest_portal":
        raise HTTPException(status_code=404, detail="Link not found.")
    if row.expires_at < datetime.utcnow():
        raise HTTPException(status_code=403, detail="This link has expired.")
    return await _ask_iris_and_log(db, row.reservation_id, body.question, request)


@router.get("/h{stem}/faq-match", response_model=FaqMatchResponse)
async def faq_match_by_prefix(
    stem: str,
    request: Request,
    q: str = "",
    db: AsyncSession = Depends(get_db),
):
    prefix = _trim_to_first4_digits(stem)
    if prefix is None:
        raise HTTPException(status_code=404, detail="Not found.")
    verified_res_id = _read_verify_cookie(request.cookies.get(VERIFY_COOKIE_NAME))
    if not verified_res_id or not verified_res_id.startswith(prefix):
        raise HTTPException(status_code=403, detail="Please re-open the portal.")
    return await _faq_match_payload(db, verified_res_id, q)


@router.post("/h{stem}/ask-iris", response_model=AskIrisResponse)
async def ask_iris_by_prefix(
    stem: str,
    body: AskIrisRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    prefix = _trim_to_first4_digits(stem)
    if prefix is None:
        raise HTTPException(status_code=404, detail="Not found.")
    verified_res_id = _read_verify_cookie(request.cookies.get(VERIFY_COOKIE_NAME))
    if not verified_res_id or not verified_res_id.startswith(prefix):
        raise HTTPException(status_code=403, detail="Please re-open the portal.")
    return await _ask_iris_and_log(db, verified_res_id, body.question, request)


# ---- FAQ-tap logging ------------------------------------------------------
#
# Fires when the guest expands a FAQ result. Creates a guest_qa row with
# llm_used=False so the staff review page sees BOTH categories of
# interaction (FAQ-tapped + LLM-asked) and can compute things like "this
# guest tapped 3 FAQ entries on the same topic then escalated to LLM"
# -- a strong signal that the FAQ answers weren't satisfying.
#
# The client de-dupes by (question, slug) tuple per page session, so we
# don't get a row every time the guest opens and closes the same
# accordion.


class FaqTapRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    slug: str = Field(min_length=1, max_length=100)


async def _log_faq_tap(
    db: AsyncSession,
    reservation_id: str,
    question: str,
    slug: str,
    request: Request,
) -> None:
    """Insert a guest_qa row for the FAQ-tap event. Best-effort -- any
    failure is logged and swallowed so the portal UI doesn't break."""
    from app.models.guest_qa import GuestQa  # noqa: PLC0415
    try:
        res = await get_reservation_by_id(reservation_id)
        check_in = (res or {}).get("check_in") or ""
        now = datetime.utcnow()
        db.add(GuestQa(
            reservation_id=reservation_id,
            asked_at=now,
            question_text=question[:1000],
            matched_faq_slugs_json=json.dumps([slug]),
            llm_used=False,
            llm_response_text=None,
            llm_input_tokens=None,
            llm_output_tokens=None,
            day_of_stay=_day_of_stay(check_in, now),
            client_ip=_client_ip(request),
            user_agent=request.headers.get("user-agent", "")[:500],
        ))
        await db.commit()
    except Exception as ex:
        log.warning("portal: log-faq-tap insert failed res=%s slug=%s: %s",
                    reservation_id, slug, ex)


@router.post("/g/{token}/log-faq-tap")
async def log_faq_tap_by_token(
    token: str,
    body: FaqTapRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(PortalToken, token)
    if row is None or row.purpose != "guest_portal":
        raise HTTPException(status_code=404, detail="Link not found.")
    if row.expires_at < datetime.utcnow():
        raise HTTPException(status_code=403, detail="This link has expired.")
    await _log_faq_tap(db, row.reservation_id, body.question, body.slug, request)
    return {"ok": True}


@router.post("/h{stem}/log-faq-tap")
async def log_faq_tap_by_prefix(
    stem: str,
    body: FaqTapRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    prefix = _trim_to_first4_digits(stem)
    if prefix is None:
        raise HTTPException(status_code=404, detail="Not found.")
    verified_res_id = _read_verify_cookie(request.cookies.get(VERIFY_COOKIE_NAME))
    if not verified_res_id or not verified_res_id.startswith(prefix):
        raise HTTPException(status_code=403, detail="Please re-open the portal.")
    await _log_faq_tap(db, verified_res_id, body.question, body.slug, request)
    return {"ok": True}


# ---- DCS-facing: guest Q&A review feed ------------------------------------
#
# Staff use this to find FAQ gaps. Two derived signals are added on top
# of the raw rows:
#
#   session_id          A short tag grouping consecutive Q&As from the
#                       same reservation that occurred within 30 minutes
#                       of each other. Multiple rows in one session ==
#                       the guest was iterating; that's the prime FAQ-
#                       improvement signal.
#
#   sim_to_previous     Jaccard similarity (0.0-1.0) of this row's
#                       question tokens to the previous row's. High
#                       similarity within a session = "guest rephrased
#                       the same question" == "FAQ didn't satisfy them."
#
# Both are computed in Python after fetch -- doable in SQL but messy,
# and we expect <1000 rows per page request.


class GuestQaLogItem(BaseModel):
    id: int
    reservation_id: str
    asked_at: datetime
    question_text: str
    matched_faq_slugs: list[str]
    # For FAQ taps the first slug here is the tapped one; for LLM rows
    # the slugs are whatever the matcher returned (may be empty).
    answer_source: str         # "faq" | "llm" | "llm_error"
    answer_text: str           # answer body (resolved from FAQ entry or LLM)
    web_search_used: bool
    day_of_stay: int | None
    llm_input_tokens: int | None
    llm_output_tokens: int | None
    reviewed_at: datetime | None
    promoted_to_kb: bool
    session_id: str            # cluster tag, derived
    sim_to_previous: float     # 0.0-1.0, derived


class GuestQaLogResponse(BaseModel):
    items: list[GuestQaLogItem]
    has_more: bool
    next_offset: int


_SESSION_GAP_SECONDS = 30 * 60


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Token-set Jaccard. 0 if either set is empty -- a 0-token query
    can't be meaningfully compared, and we don't want a false 1.0 from
    two empty sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _annotate_sessions(rows: list) -> list[tuple[object, str, float]]:
    """Given GuestQa rows in arbitrary order, return list of
    (row, session_id, sim_to_previous) annotations.

    Sessions group by reservation_id with a max gap of
    _SESSION_GAP_SECONDS between consecutive questions. Similarity is
    measured against the IMMEDIATELY preceding question in the same
    session (token-set Jaccard with synonym expansion to align with how
    the matcher sees the input)."""
    from collections import defaultdict
    from app.services.faq_match import tokens_for_text  # noqa: PLC0415

    by_res: dict[str, list] = defaultdict(list)
    for r in rows:
        by_res[r.reservation_id].append(r)

    annotated: list[tuple[object, str, float]] = []
    for res_id, res_rows in by_res.items():
        res_rows.sort(key=lambda r: r.asked_at)
        prev_row = None
        prev_tokens: frozenset[str] | None = None
        session_idx = 0
        for r in res_rows:
            tokens = tokens_for_text(r.question_text, expand=True)
            if prev_row is None:
                session_idx = 1
                sim = 0.0
            else:
                gap = (r.asked_at - prev_row.asked_at).total_seconds()
                if gap > _SESSION_GAP_SECONDS:
                    session_idx += 1
                    sim = 0.0
                else:
                    sim = _jaccard(tokens, prev_tokens) if prev_tokens else 0.0
            sid = f"{res_id[-6:]}-{session_idx}"
            annotated.append((r, sid, sim))
            prev_row = r
            prev_tokens = tokens
    return annotated


def _resolve_answer(
    row, faq_lookup: dict[str, "object"],
) -> tuple[str, str, bool]:
    """Return (source, text, web_search_used) for one row.

    For FAQ-tap rows: source="faq", text is the answer body of the
    tapped FAQ entry (looked up via slug). For LLM rows: source="llm"
    (or "llm_error" if the call failed), text is the response. Empty
    answer text falls back to a placeholder so the table never shows
    a blank cell."""
    if row.llm_used:
        if row.llm_response_text:
            return "llm", row.llm_response_text, False
        # No response = LLM call failed
        return "llm_error", "(LLM call failed -- see logs)", False
    try:
        slugs = json.loads(row.matched_faq_slugs_json or "[]")
    except (ValueError, TypeError):
        slugs = []
    if slugs and isinstance(slugs, list) and slugs[0] in faq_lookup:
        entry = faq_lookup[slugs[0]]
        return "faq", entry.answer, False
    return "faq", "(FAQ entry no longer in knowledge base)", False


@router.get(
    "/portal/guest-qa-log",
    response_model=GuestQaLogResponse,
    dependencies=[Depends(require_portal_auth)],
    tags=["portal"],
)
async def get_guest_qa_log(
    since_days: int = 7,
    reservation_id: str | None = None,
    unreviewed_only: bool = False,
    limit: int = 200,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """Staff feed of guest portal questions. Filters:
      - since_days: only rows from the last N days (default 7)
      - reservation_id: restrict to one reservation
      - unreviewed_only: hide rows already marked reviewed
      - limit / offset: pagination (defaults 200 / 0)

    Returns rows in `asked_at DESC` order with session_id + similarity
    annotations so the DCS UI can flag iterative guests."""
    from app.models.guest_qa import GuestQa  # noqa: PLC0415
    from app.services.faq_kb import get_faq_entries  # noqa: PLC0415

    if limit < 1: limit = 1
    if limit > 500: limit = 500
    if offset < 0: offset = 0
    if since_days < 1: since_days = 1
    if since_days > 365: since_days = 365

    cutoff = datetime.utcnow() - timedelta(days=since_days)
    stmt = select(GuestQa).where(GuestQa.asked_at >= cutoff)
    if reservation_id:
        stmt = stmt.where(GuestQa.reservation_id == reservation_id)
    if unreviewed_only:
        stmt = stmt.where(GuestQa.reviewed_at.is_(None))
    stmt = stmt.order_by(GuestQa.asked_at.desc()).limit(limit + 1).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    annotated = _annotate_sessions(rows)
    # Re-sort to newest-first (session grouping above ascending-sorted them)
    annotated.sort(key=lambda t: -t[0].asked_at.timestamp())

    faq_lookup = {e.slug: e for e in get_faq_entries()}
    items: list[GuestQaLogItem] = []
    for row, sid, sim in annotated:
        try:
            slugs = json.loads(row.matched_faq_slugs_json or "[]")
            if not isinstance(slugs, list):
                slugs = []
        except (ValueError, TypeError):
            slugs = []
        source, text, web_used = _resolve_answer(row, faq_lookup)
        # The "web_search_used" flag isn't stored separately yet (we
        # could; it's derivable from llm_response inspection too). For
        # now expose False from the resolver; future row addition can
        # populate this.
        items.append(GuestQaLogItem(
            id=row.id,
            reservation_id=row.reservation_id,
            asked_at=row.asked_at,
            question_text=row.question_text,
            matched_faq_slugs=[s for s in slugs if isinstance(s, str)],
            answer_source=source,
            answer_text=text,
            web_search_used=web_used,
            day_of_stay=row.day_of_stay,
            llm_input_tokens=row.llm_input_tokens,
            llm_output_tokens=row.llm_output_tokens,
            reviewed_at=row.reviewed_at,
            promoted_to_kb=bool(row.promoted_to_kb),
            session_id=sid,
            sim_to_previous=round(sim, 3),
        ))
    return GuestQaLogResponse(
        items=items, has_more=has_more, next_offset=offset + limit,
    )


class GuestQaReviewRequest(BaseModel):
    promoted_to_kb: bool = False


@router.post(
    "/portal/guest-qa/{qa_id}/review",
    dependencies=[Depends(require_portal_auth)],
    tags=["portal"],
)
async def mark_guest_qa_reviewed(
    qa_id: int,
    body: GuestQaReviewRequest,
    db: AsyncSession = Depends(get_db),
):
    """Staff marks a row as reviewed (and optionally promoted-to-KB).
    Idempotent -- re-reviewing updates the timestamp but doesn't break
    anything."""
    from app.models.guest_qa import GuestQa  # noqa: PLC0415
    row = await db.get(GuestQa, qa_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Q&A row not found.")
    row.reviewed_at = datetime.utcnow()
    if body.promoted_to_kb:
        row.promoted_to_kb = True
    await db.commit()
    return {"ok": True, "reviewed_at": row.reviewed_at.isoformat()}


# DCS-facing read: returns the prioritized list (and the canonical
# label/tip metadata) for one reservation. Used by the DCS Reservations
# and Rooms pages to show the guest's prefs during morning-of room
# assignment. Shared-secret auth (X-Portal-Auth header) -- the same guard
# as the rest of the DCS-facing portal endpoints.


class _GuestPrefItem(BaseModel):
    key: str
    label: str
    tip: str
    priority: int  # 1-based; 1 = most important to the guest


class GuestPrefsResponse(BaseModel):
    reservation_id: str
    saved: bool                      # False = guest never engaged with the section
    locked: bool                     # True = stay is past the editable window
    items: list[_GuestPrefItem]      # in guest's stated priority order
    updated_at: datetime | None


@router.get(
    "/portal/guest-preferences/{reservation_id}",
    response_model=GuestPrefsResponse,
    dependencies=[Depends(require_portal_auth)],
    tags=["portal"],
)
async def get_guest_preferences(
    reservation_id: str,
    db: AsyncSession = Depends(get_db),
):
    """DCS staff view: pull a reservation's prioritized preference list
    for display next to the booking when assigning rooms. Returns an
    empty list with saved=False when the guest never engaged with the
    section -- callers can show 'No preferences set' in that case
    without distinguishing it from a load error.

    `locked` indicates whether the guest can still edit. It's purely
    informational -- doesn't change the response shape -- but lets the
    staff UI say 'preferences (locked)' once the stay starts."""
    from app.models.guest_preference import GuestPreference
    row = await db.get(GuestPreference, reservation_id)
    saved = row is not None
    try:
        keys: list[str] = json.loads(row.prioritized_json or "[]") if row else []
    except (ValueError, TypeError):
        keys = []
    if not isinstance(keys, list):
        keys = []
    res = await get_reservation_by_id(reservation_id)
    phase = (res or {}).get("stay_phase") or "unknown"
    locked = phase not in _PREFS_EDITABLE_PHASES
    valid = valid_keys()
    items: list[_GuestPrefItem] = []
    priority = 0
    for k in keys:
        if not isinstance(k, str) or k not in valid:
            continue
        pref = next((p for p in AVAILABLE_PREFS if p.key == k), None)
        if pref is None:
            continue
        priority += 1
        items.append(_GuestPrefItem(
            key=pref.key, label=pref.label, tip=pref.tip, priority=priority,
        ))
    return GuestPrefsResponse(
        reservation_id=reservation_id,
        saved=saved,
        locked=locked,
        items=items,
        updated_at=row.updated_at if row else None,
    )


class GuestPrefsBatchResponse(BaseModel):
    """Result of the batch preferences fetch -- one entry per reservation
    in the input, in the same order. Missing rows (guest never engaged
    with the section) come back with saved=False and items=[]."""
    items: dict[str, GuestPrefsResponse]


@router.get(
    "/portal/guest-preferences-batch",
    response_model=GuestPrefsBatchResponse,
    dependencies=[Depends(require_portal_auth)],
    tags=["portal"],
)
async def get_guest_preferences_batch(
    reservation_ids: str = "",
    db: AsyncSession = Depends(get_db),
):
    """Bulk preferences fetch for the DCS Reservations + Rooms pages.
    Each page typically renders 10-30 reservations at once; we batch
    into a single DB query + one Cloudbeds-status check to avoid the
    N+1 HTTP roundtrip we'd get from calling /guest-preferences/{id} in
    a loop.

    Input is a comma-separated string of reservation IDs (URL-safe).
    Empty -> empty response. Unknown IDs come back as saved=False
    entries so the caller can render "no prefs yet" uniformly without
    distinguishing "no row" from "no input."""
    from app.models.guest_preference import GuestPreference  # noqa: PLC0415

    ids = [s.strip() for s in reservation_ids.split(",") if s.strip()]
    # Cap to keep one bad URL from hammering the DB. Sanity bound only;
    # the Reservations page never asks for more than the count of active
    # reservations, which is ~30 max at this property.
    if len(ids) > 200:
        ids = ids[:200]
    if not ids:
        return GuestPrefsBatchResponse(items={})

    # One query: all matching pref rows
    stmt = select(GuestPreference).where(GuestPreference.reservation_id.in_(ids))
    rows = {r.reservation_id: r for r in (await db.execute(stmt)).scalars().all()}

    # Phase lookup needs a Cloudbeds call per reservation -- we batch by
    # firing them concurrently via gather. ~30 reservations * ~50ms
    # cached = 1.5s if we did them serially; concurrent gather brings
    # that under a second. The reservations cache (30s TTL in this same
    # module) usually makes these near-free anyway.
    import asyncio  # noqa: PLC0415
    phases: dict[str, str] = {}
    async def _phase_for(rid: str) -> tuple[str, str]:
        res = await get_reservation_by_id(rid)
        return rid, (res or {}).get("stay_phase") or "unknown"
    for rid, phase in await asyncio.gather(*[_phase_for(rid) for rid in ids]):
        phases[rid] = phase

    valid = valid_keys()
    out: dict[str, GuestPrefsResponse] = {}
    for rid in ids:
        row = rows.get(rid)
        saved = row is not None
        try:
            keys: list[str] = json.loads(row.prioritized_json or "[]") if row else []
        except (ValueError, TypeError):
            keys = []
        if not isinstance(keys, list):
            keys = []
        phase = phases.get(rid, "unknown")
        locked = phase not in _PREFS_EDITABLE_PHASES
        items: list[_GuestPrefItem] = []
        priority = 0
        for k in keys:
            if not isinstance(k, str) or k not in valid:
                continue
            pref = next((p for p in AVAILABLE_PREFS if p.key == k), None)
            if pref is None:
                continue
            priority += 1
            items.append(_GuestPrefItem(
                key=pref.key, label=pref.label, tip=pref.tip, priority=priority,
            ))
        out[rid] = GuestPrefsResponse(
            reservation_id=rid,
            saved=saved,
            locked=locked,
            items=items,
            updated_at=row.updated_at if row else None,
        )
    return GuestPrefsBatchResponse(items=out)


# ---- Signature agreement ----------------------------------------------------

async def _apply_signature(
    reservation_id: str,
    request: Request,
    db: AsyncSession,
    *,
    typed_name: str,
    signature_png_data_url: str,
) -> tuple[bool, str]:
    """Generate the agreement PDF, attach to Cloudbeds, and write the local
    audit row. Returns (ok, error_message).

    Idempotent: if a SignatureAgreement row already exists for the
    reservation, returns success without re-signing (caller's form should
    already be showing the read-only summary, but the redirect+race is
    defended-against here too)."""
    from app.models.signature_agreement import SignatureAgreement
    from app.tools.pdf_generator import (
        _decode_signature_png, signature_png_looks_drawn, render_agreement_pdf,
    )

    if not reservation_id:
        return False, "Couldn't identify the reservation."

    # One-time only -- silently no-op on duplicate POSTs (race / refresh).
    existing = await _latest_signature_agreement(db, reservation_id)
    if existing is not None:
        return True, ""

    png_bytes = _decode_signature_png(signature_png_data_url)
    if not signature_png_looks_drawn(png_bytes):
        return False, "Please draw your signature in the box before signing."

    # Pull reservation details for the PDF header
    res = await get_reservation_by_id(reservation_id)
    guest_name = (res.get("guest_name") if res else "") or ""
    check_in = (res.get("check_in") if res else "") or ""
    check_out = (res.get("check_out") if res else "") or ""

    signed_at = datetime.utcnow()
    pdf_bytes = render_agreement_pdf(
        hotel_name=settings.hotel_name,
        hotel_address=settings.hotel_address,
        hotel_phone=settings.hotel_phone_display,
        guest_name=guest_name,
        reservation_id=reservation_id,
        check_in=check_in,
        check_out=check_out,
        agreement_text=AGREEMENT_TEXT,
        agreement_version=AGREEMENT_VERSION,
        typed_name=typed_name or guest_name,
        signature_png=png_bytes,
        signed_at_utc=signed_at,
    )

    filename = f"rental-agreement-{reservation_id}-{signed_at.strftime('%Y%m%d-%H%M%S')}.pdf"
    upload = await post_reservation_document(reservation_id, pdf_bytes, filename)
    cloudbeds_ok = bool(upload.get("success"))
    cloudbeds_doc_id = upload.get("doc_id") if cloudbeds_ok else None

    # We always write the local row -- even on Cloudbeds failure -- so the
    # signature isn't lost. A retry job can later re-upload using the
    # stored signature PNG + agreement text.
    db.add(SignatureAgreement(
        reservation_id=reservation_id,
        guest_name=guest_name,
        typed_name=typed_name or None,
        agreement_text=AGREEMENT_TEXT,
        agreement_version=AGREEMENT_VERSION,
        signature_png_base64=signature_png_data_url,  # data URL form -- self-describing
        cloudbeds_attached=cloudbeds_ok,
        cloudbeds_doc_id=cloudbeds_doc_id,
        cloudbeds_attached_at=signed_at if cloudbeds_ok else None,
        signed_at=signed_at,
        client_ip=_client_ip(request),
        user_agent=request.headers.get("user-agent", "")[:500],
    ))
    await db.commit()
    log.info("portal: signature recorded for res=%s name=%s attached=%s",
             reservation_id, typed_name or guest_name, cloudbeds_ok)

    if not cloudbeds_ok:
        # Signed locally but upload failed. Still surface as success-ish so
        # the guest sees confirmation; the warn banner explains the rest.
        return True, "uploaded_locally"
    return True, ""


@router.post("/g/{token}/sign")
async def post_sign_by_token(
    token: str,
    request: Request,
    typed_name: str = Form(default=""),
    signature_png: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(PortalToken, token)
    if row is None or row.purpose != "guest_portal":
        return _portal_page("Not found", _NOT_FOUND_PAGE)
    if row.expires_at < datetime.utcnow():
        return _portal_page("Expired", _EXPIRED_PAGE)
    ok, code_extra = await _apply_signature(
        row.reservation_id, request, db,
        typed_name=typed_name, signature_png_data_url=signature_png,
    )
    code = "sign" if ok and code_extra == "" else ("sign_uploaded_locally" if ok else "sign_error")
    return RedirectResponse(url=f"/g/{token}?saved={code}#sign-section", status_code=303)


@router.post("/h{stem}/sign")
async def post_sign_by_prefix(
    stem: str,
    request: Request,
    typed_name: str = Form(default=""),
    signature_png: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    prefix = _trim_to_first4_digits(stem)
    if prefix is None:
        return _portal_page("Not found", _NOT_FOUND_PAGE)
    verified_res_id = _read_verify_cookie(request.cookies.get(VERIFY_COOKIE_NAME))
    if not verified_res_id or not verified_res_id.startswith(prefix):
        log.warning("portal: sign POST rejected ip=%s prefix=%s cookie=%s",
                    _client_ip(request), prefix, "yes" if verified_res_id else "no")
        return RedirectResponse(url=f"/h{prefix}", status_code=303)
    ok, code_extra = await _apply_signature(
        verified_res_id, request, db,
        typed_name=typed_name, signature_png_data_url=signature_png,
    )
    code = "sign" if ok and code_extra == "" else ("sign_uploaded_locally" if ok else "sign_error")
    return RedirectResponse(url=f"/h{prefix}?saved={code}#sign-section", status_code=303)


# ---- Card on file (Cloudbeds-tokenized) ------------------------------------
#
# Why no Stripe SDK on our side: this property uses "Cloudbeds Payments"
# (StripePlatformGateway), meaning Cloudbeds is the Stripe merchant of
# record and any PaymentMethods we'd mint on a separate Stripe account
# would be unusable. The proven path (matching the GX-26 wrapper) is to
# POST raw card fields to Cloudbeds' postCreditCard endpoint over TLS;
# Cloudbeds handles tokenization against their Stripe Connect account.
#
# PCI scope: the card data flows browser -> our server -> Cloudbeds. We
# never log it, never persist it, and the variable goes out of scope after
# the call returns. That matches what GX-26 has been doing in production
# against this same Cloudbeds property.

def _normalize_card_expiration(exp_month: str, exp_year: str) -> str | None:
    """Build a 'MM/YY' string from raw form input. Returns None on invalid
    month/year or on an expired card. Mirrors GX-26's NormalizeExpiration
    so we don't pass garbage through to Cloudbeds for a clearer error."""
    from datetime import date
    try:
        m = int(exp_month)
        y = int(exp_year)
    except (TypeError, ValueError):
        return None
    if not (1 <= m <= 12):
        return None
    # Accept 4-digit (2027) or 2-digit (27) year input.
    if y >= 2000 and y < 2100:
        y = y - 2000
    if not (20 <= y <= 99):
        return None
    today = date.today()
    now_y = today.year - 2000
    if y < now_y or (y == now_y and m < today.month):
        return None
    return f"{m:02d}/{y:02d}"


async def _apply_card_attach(
    reservation_id: str,
    *,
    card_holder_name: str,
    card_number: str,
    exp_month: str,
    exp_year: str,
    card_cvv: str,
    card_address_zip: str,
) -> tuple[bool, str]:
    """[DEPRECATED 2026-05-27] Raw-PAN forward to Cloudbeds postCreditCard.

    Replaced by the Stripe Elements -> dashboard_save_credit_card path
    (see _save_card_via_dashboard above). The dashboard endpoint reliably
    produces a card the front desk can charge from the UI; postCreditCard
    sometimes attaches a card that doesn't show up there. Kept as
    callable code in case we ever need a back-office card-attach path
    that bypasses the browser (e.g. admin tool calling from the server).

    Returns (ok, error_message). Caller is responsible for scrubbing the
    inputs after this returns (they pass through here once)."""
    # Strip whitespace + dashes from PAN so the user can paste "4242 4242..."
    pan_clean = "".join(c for c in (card_number or "") if c.isdigit())
    cvv_clean = "".join(c for c in (card_cvv or "") if c.isdigit())
    if not (13 <= len(pan_clean) <= 19):
        return False, "Card number must be 13-19 digits."
    if not (3 <= len(cvv_clean) <= 4):
        return False, "CVV must be 3 or 4 digits."
    expiration = _normalize_card_expiration(exp_month, exp_year)
    if expiration is None:
        return False, "Expiration is invalid or in the past."

    # GX-26 always sends cardType -- detect from BIN since the form doesn't
    # ask the guest to pick a brand. If detection fails (unusual BIN),
    # leave the field unset and let Cloudbeds figure it out.
    card_brand = detect_card_type_from_pan(pan_clean)
    result = await post_credit_card(
        reservation_id,
        card_number=pan_clean,
        card_expiration=expiration,
        card_cvv=cvv_clean,
        card_holder_name=card_holder_name.strip() or None,
        card_type=card_brand,
        card_address_zip=(card_address_zip or "").strip() or None,
    )
    # Scrub locals -- best-effort; Python's GC will eventually collect, but
    # explicit overwrite makes intent clear in audit + protects against any
    # accidental future logging-of-locals.
    pan_clean = cvv_clean = ""
    if not result.get("success"):
        return False, result.get("error") or "Could not save the card."
    _invalidate_reservations_cache()
    log.info("portal: card attached for res=%s card_id=%s",
             reservation_id, result.get("card_id") or "?")
    return True, ""


# ---- Card save: JSON in, JSON out -----------------------------------------
#
# Both /g/{token}/cards and /h{stem}/cards accept a Stripe.js legacy token
# (`tok_xxx`) plus the token's `card` metadata block. We resolve booking_id
# from reservation_id, then call dashboard_save_credit_card -- which posts
# to https://hotels.cloudbeds.com/hotel/save_credit_card with a cookie
# session and gets back a Cloudbeds card_id. The browser's Stripe.js had
# already done the tokenization, so PAN/CVV never reach our backend.
#
# Why JSON instead of form-POST + 303 redirect (the pattern used by other
# portal sections): Stripe Elements is JavaScript-driven and the form
# submission already runs through Stripe.createToken on the client. Once
# JS is in the loop, returning JSON and letting JS handle the
# success-reload / error-display is simpler than coercing the response
# into a redirect that JS would have to re-detect.


class CardSaveRequest(BaseModel):
    """JSON body for the card-save POST. token_id is the Stripe legacy
    token (tok_xxx); token_card is Stripe's `card` metadata (brand, last4,
    exp_month, exp_year, etc.) which we forward to Cloudbeds for the
    visible card list."""
    token_id: str = Field(min_length=1, description="Stripe legacy token id (tok_xxx)")
    token_card: dict = Field(default_factory=dict)


async def _save_card_via_dashboard(reservation_id: str, body: CardSaveRequest) -> JSONResponse:
    """Shared body of the /g and /h POST endpoints. Looks up booking_id,
    forwards the tokenized card to /hotel/save_credit_card, returns a JSON
    response the JS form handler understands."""
    booking_id = await get_booking_id(reservation_id)
    if not booking_id:
        log.warning("portal cards: couldn't resolve booking_id for reservation=%s", reservation_id)
        return JSONResponse(
            {"success": False, "error": "We couldn't find the reservation. Please contact the front desk."},
            status_code=400,
        )

    log.info(
        "portal cards: reservation=%s booking_id=%s stripe_token=%s last4=%s",
        reservation_id, booking_id, body.token_id, body.token_card.get("last4"),
    )

    result = await dashboard_save_credit_card(
        booking_id=booking_id,
        legacy_token_id=body.token_id,
        token_card=body.token_card,
    )

    if result.get("success"):
        _invalidate_reservations_cache()
        log.info(
            "portal cards: success card_id=%s booking=%s",
            result.get("card_id"), booking_id,
        )
        return JSONResponse({
            "success": True,
            "card_id": result.get("card_id"),
            "last4": (result.get("card_details") or {}).get("card_number"),
        })

    log.warning("portal cards: dashboard_save_credit_card rejected: %s",
                json.dumps(result, default=str)[:500])
    return JSONResponse({
        "success": False,
        "error": "We couldn't save your card. Please try again, or contact the front desk.",
    }, status_code=400)


@router.post("/g/{token}/cards")
async def post_card_by_token(
    token: str,
    body: CardSaveRequest,
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(PortalToken, token)
    if row is None or row.purpose != "guest_portal":
        return JSONResponse({"success": False, "error": "Link not found."}, status_code=404)
    if row.expires_at < datetime.utcnow():
        return JSONResponse({"success": False, "error": "This link has expired."}, status_code=403)
    return await _save_card_via_dashboard(row.reservation_id, body)


@router.post("/h{stem}/cards")
async def post_card_by_prefix(
    stem: str,
    body: CardSaveRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    prefix = _trim_to_first4_digits(stem)
    if prefix is None:
        return JSONResponse({"success": False, "error": "Not found."}, status_code=404)
    verified_res_id = _read_verify_cookie(request.cookies.get(VERIFY_COOKIE_NAME))
    if not verified_res_id or not verified_res_id.startswith(prefix):
        log.warning("portal: card POST rejected ip=%s prefix=%s cookie=%s",
                    _client_ip(request), prefix, "yes" if verified_res_id else "no")
        return JSONResponse(
            {"success": False, "error": "Please re-open the portal from your SMS link."},
            status_code=403,
        )
    return await _save_card_via_dashboard(verified_res_id, body)


# ---- Cloudbeds Pay-by-Link (UI-automation generated) -----------------------

async def _get_or_generate_pay_by_link(
    reservation_id: str, request: Request, db: AsyncSession,
) -> dict:
    """Return a usable Pay-by-Link URL for this reservation. Uses the cached
    one when it exists + hasn't expired; otherwise drives the Cloudbeds
    dashboard via Playwright to mint a new one. Always writes a row to
    pay_by_link (success or failure) so we have audit history."""
    from app.models.pay_by_link import PayByLink
    from app.tools.cloudbeds_browser import generate_pay_by_link
    from sqlalchemy import select

    # Cached link still good?
    stmt = (
        select(PayByLink)
        .where(PayByLink.reservation_id == reservation_id)
        .where(PayByLink.url.is_not(None))
        .order_by(PayByLink.id.desc())
        .limit(1)
    )
    cached = (await db.execute(stmt)).scalar_one_or_none()
    if cached and cached.expires_at and cached.expires_at > datetime.utcnow():
        log.info("portal: pay-by-link cache HIT for res=%s (expires %s)",
                 reservation_id, cached.expires_at.isoformat())
        return {"success": True, "url": cached.url, "expires_at": cached.expires_at, "cached": True}

    log.info("portal: pay-by-link cache MISS for res=%s -- generating", reservation_id)
    result = await generate_pay_by_link(
        reservation_id, client_ip=_client_ip(request),
    )
    # Persist outcome -- on failure too, so audit is complete.
    db.add(PayByLink(
        reservation_id=reservation_id,
        url=result.get("url") if result.get("success") else None,
        expires_at=result.get("expires_at"),
        generation_method="ui_automation",
        error_message=None if result.get("success") else (result.get("error") or "Unknown failure"),
        client_ip=_client_ip(request),
    ))
    await db.commit()
    return result


def _render_card_link_page(prefix_or_token: str, result: dict, return_path: str) -> HTMLResponse:
    """Render the page that surrounds the Pay-by-Link iframe (or shows the
    failure message). Same shell as the rest of the portal but stripped
    down -- the focus is the iframe + a way back."""
    if not result.get("success"):
        err = result.get("error") or "We couldn't generate a card-entry link right now."
        body = f"""
<h1>Add a card</h1>
<div class="gated-prompt">
    <strong>Something went wrong on our end.</strong> {_esc(err)}
    <p style="margin-top:10px;">Please call the front desk at
    <a href="tel:{settings.hotel_phone_tel}">{settings.hotel_phone_display}</a>
    and we'll add the card for you.</p>
</div>
<p><a href="{return_path}">&larr; Back to your stay</a></p>
"""
        return _portal_page("Add a card", body)

    url = result["url"]
    cached = result.get("cached")
    body = f"""
<h1>Add a card</h1>
<p>Enter your card on the secure page below. The page is hosted by our
   reservation system &mdash; your card details go directly to them; we
   never see your card number.</p>
{('<p class="card-info" style="margin-top:0;">Reusing a link generated earlier today.</p>' if cached else '')}
<iframe class="card-link-frame" src="{_esc(url)}" allow="payment"></iframe>
<div class="card-link-fallback">
    If the form above doesn't load, your browser may be blocking embedded
    pages from our reservation system.
    <a href="{_esc(url)}" target="_blank" rel="noopener">Open the card-entry page in a new tab</a>.
</div>
<p style="margin-top:16px;"><a href="{return_path}">&larr; Back to your stay</a></p>
"""
    return _portal_page("Add a card", body)


def _render_loading_page(target_action_url: str, return_path: str) -> HTMLResponse:
    """Brief 'Generating your link...' splash that auto-POSTs to the action
    URL. We use a separate POST endpoint that actually does the Playwright
    work, so the GET request to this page returns instantly and the
    browser has a chance to render the spinner before the slow op fires."""
    body = f"""
<h1>Add a card</h1>
<div class="card-link-loading">
    <div class="spinner"></div>
    <p>Generating your secure card-entry link...</p>
    <p style="font-size:13px;">This can take up to 30 seconds.</p>
</div>
<form id="auto-form" method="post" action="{target_action_url}"></form>
<script>document.getElementById('auto-form').submit();</script>
<noscript>
    <p>Your browser has JavaScript disabled.
    <a href="{return_path}">Go back</a> and please call the front desk to add a card.</p>
</noscript>
"""
    return _portal_page("Add a card", body)


@router.get("/g/{token}/card-link")
async def get_card_link_by_token(
    token: str, request: Request, db: AsyncSession = Depends(get_db),
):
    row = await db.get(PortalToken, token)
    if row is None or row.purpose != "guest_portal":
        return _portal_page("Not found", _NOT_FOUND_PAGE)
    if row.expires_at < datetime.utcnow():
        return _portal_page("Expired", _EXPIRED_PAGE)
    return _render_loading_page(
        target_action_url=f"/g/{token}/card-link",
        return_path=f"/g/{token}",
    )


@router.post("/g/{token}/card-link")
async def post_card_link_by_token(
    token: str, request: Request, db: AsyncSession = Depends(get_db),
):
    row = await db.get(PortalToken, token)
    if row is None or row.purpose != "guest_portal":
        return _portal_page("Not found", _NOT_FOUND_PAGE)
    if row.expires_at < datetime.utcnow():
        return _portal_page("Expired", _EXPIRED_PAGE)
    result = await _get_or_generate_pay_by_link(row.reservation_id, request, db)
    return _render_card_link_page(token, result, return_path=f"/g/{token}")


@router.get("/h{stem}/card-link")
async def get_card_link_by_prefix(
    stem: str, request: Request, db: AsyncSession = Depends(get_db),
):
    prefix = _trim_to_first4_digits(stem)
    if prefix is None:
        return _portal_page("Not found", _NOT_FOUND_PAGE)
    verified_res_id = _read_verify_cookie(request.cookies.get(VERIFY_COOKIE_NAME))
    if not verified_res_id or not verified_res_id.startswith(prefix):
        return RedirectResponse(url=f"/h{prefix}", status_code=303)
    return _render_loading_page(
        target_action_url=f"/h{prefix}/card-link",
        return_path=f"/h{prefix}",
    )


@router.post("/h{stem}/card-link")
async def post_card_link_by_prefix(
    stem: str, request: Request, db: AsyncSession = Depends(get_db),
):
    prefix = _trim_to_first4_digits(stem)
    if prefix is None:
        return _portal_page("Not found", _NOT_FOUND_PAGE)
    verified_res_id = _read_verify_cookie(request.cookies.get(VERIFY_COOKIE_NAME))
    if not verified_res_id or not verified_res_id.startswith(prefix):
        return RedirectResponse(url=f"/h{prefix}", status_code=303)
    result = await _get_or_generate_pay_by_link(verified_res_id, request, db)
    return _render_card_link_page(prefix, result, return_path=f"/h{prefix}")
