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
from app.models.portal_token import PortalToken
from app.tools.cloudbeds import _extract_phones_from_reservation, _get, _summarize_reservation, add_reservation_note, detect_card_type_from_pan, format_phone_display, get_reservation_by_id, normalize_phone_e164, post_credit_card, post_item, post_reservation_document, post_void_item, put_guest_contact
from app.tools.twilio_sms import get_message_status, send_sms

log = logging.getLogger(__name__)
router = APIRouter()

TOKEN_LIFETIME = timedelta(hours=24)


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


@router.post("/portal/issue-guest-token", response_model=IssueGuestTokenResponse,
             dependencies=[Depends(require_portal_auth)])
async def issue_guest_token(req: IssueGuestTokenRequest, db: AsyncSession = Depends(get_db)):
    """Get-or-create the long-lived guest-portal token for a reservation.

    Idempotent: returns the existing token if one is still valid; otherwise
    creates a new one. The token URL goes into the pre-arrival SMS that DCS
    sends; the same URL stays usable through 24h after checkout.
    """
    now = datetime.utcnow()
    existing = (await db.execute(
        select(PortalToken)
        .where(PortalToken.purpose == "guest_portal")
        .where(PortalToken.reservation_id == req.reservation_id)
        .where(PortalToken.expires_at > now)
        .order_by(PortalToken.created_at.desc())
    )).scalar_one_or_none()
    if existing:
        return IssueGuestTokenResponse(
            token=existing.token,
            portal_url=f"{settings.portal_public_base_url.rstrip('/')}/g/{existing.token}",
            reservation_id=req.reservation_id,
            is_new=False,
        )

    # Look up the reservation just to capture first_name + room_name for SMS
    # templating. The page re-fetches Cloudbeds live on each visit so anything
    # else is read fresh; we don't snapshot stay dates here. 60-day token
    # lifetime is wide enough to cover any reasonable advance booking.
    res = await get_reservation_by_id(req.reservation_id)
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
        reservation_id=req.reservation_id,
        first_name=first_name,
        room_number=room_name,  # storing Cloudbeds name; page does the friendly map
        created_at=now,
        expires_at=expires_at,
    )
    db.add(row)
    await db.commit()

    log.info("portal: issued guest_portal token for res=%s (expires %s)",
             req.reservation_id, expires_at.isoformat())
    return IssueGuestTokenResponse(
        token=token,
        portal_url=f"{settings.portal_public_base_url.rstrip('/')}/g/{token}",
        reservation_id=req.reservation_id,
        is_new=True,
    )


# ---- Stay-phase → human-friendly status copy --------------------------------

def _status_for_phase(phase: str, status: str, check_in: str | None, check_out: str | None) -> dict:
    """Map a (stay_phase, Cloudbeds-status) pair to (badge_text, badge_class,
    headline, body). Drives the colored status pill + paragraph at the top
    of the portal page. Returned dict keys are used directly by the template."""
    s = (status or "").lower()
    if s in {"canceled", "cancelled", "no_show"}:
        return {
            "badge": "canceled", "badge_class": "muted",
            "headline": "Your reservation was canceled.",
            "body": "If this was unexpected, please call the front desk at (541) 997-3221.",
        }
    if phase == "future":
        return {
            "badge": "Upcoming", "badge_class": "info",
            "headline": f"We look forward to seeing you on {check_in or 'your arrival date'}.",
            "body": "You can return to this page any time before your stay to update preferences, "
                    "confirm contact info, or add an incidentals card. Sections below will unlock as "
                    "we add them.",
        }
    if phase == "arriving_today":
        return {
            "badge": "Arriving today", "badge_class": "primary",
            "headline": "We're expecting you today!",
            "body": "Standard check-in is 4:00 PM. If your room is ready earlier, we'll let you know "
                    "on this page. Once you've completed the items below, we can text you the door "
                    "code so you can go straight to your room.",
        }
    if phase == "in_house":
        return {
            "badge": "Currently staying", "badge_class": "primary",
            "headline": "You're staying with us.",
            "body": "WiFi info and stay-related actions are below.",
        }
    if phase == "in_house_departing_tomorrow":
        return {
            "badge": "Departing tomorrow", "badge_class": "primary",
            "headline": "We hope you've enjoyed your stay so far.",
            "body": f"Check-out tomorrow ({check_out or ''}) is at 11:00 AM. "
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
.hotel-meta { font-size: 13px; color: #475569; margin-top: 2px; }
.hotel-meta a { color: #1e40af; text-decoration: none; }
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
.footer { text-align: center; color: #94a3b8; font-size: 12px; margin: 20px 0 8px; }
.footer a { color: #64748b; }
"""


def _hotel_header_html() -> str:
    """Pinned header with hotel name, address, phone -- shown on every portal-style page."""
    parts = [f'<div class="hotel-name">{settings.hotel_name}</div>']
    if settings.hotel_address:
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
<h1>Hi {first_name}!</h1>
{saved_banner_html}
<div class="section">
    <span class="badge {badge_class}">{badge}</span>
    <div class="headline">{headline}</div>
    <p>{body}</p>
    <dl class="kv">
        <dt>Arriving</dt><dd>{check_in}</dd>
        <dt>Departing</dt><dd>{check_out}</dd>
    </dl>
    {countdown_html}
</div>

<details class="accord" open>
    <summary>🚪 Room &amp; door code</summary>
    <div class="accord-body">
        {room_block}
    </div>
</details>

<details class="accord">
    <summary>📶 WiFi</summary>
    <div class="accord-body">
        <p>Network: <span class="wifi-creds">LighthouseInn</span></p>
        <p>Password: <span class="wifi-creds">happyguest</span></p>
    </div>
</details>

<details class="accord">
    <summary>👤 Your info</summary>
    <div class="accord-body">
        <dl class="kv">
            <dt>Guest</dt><dd>{guest_name}</dd>
            <dt>Reservation</dt><dd>{reservation_id}</dd>
        </dl>
    </div>
</details>

<details class="accord {contact_complete_class}" id="contact-section" {contact_open_attr}>
    <summary>
        <span class="accord-title">📋 Confirm address &amp; phone</span>
        <span class="accord-check" aria-label="completed">✓</span>
    </summary>
    <div class="accord-body">
        {contact_block}
    </div>
</details>

<details class="accord {sign_complete_class}" id="sign-section" {sign_open_attr}>
    <summary>
        <span class="accord-title">✍️ Sign the rental agreement</span>
        <span class="accord-check" aria-label="completed">✓</span>
    </summary>
    <div class="accord-body">
        {sign_block}
    </div>
</details>

<details class="accord {card_complete_class}" id="card-section" {card_open_attr}>
    <summary>
        <span class="accord-title">💳 Credit card on file</span>
        <span class="accord-check" aria-label="completed">✓</span>
    </summary>
    <div class="accord-body">
        {card_block}
    </div>
</details>

<details class="accord {pet_complete_class}" id="pet-section" {pet_open_attr}>
    <summary>
        <span class="accord-title">🐕 Bringing a pet?</span>
        <span class="accord-check" aria-label="completed">✓</span>
    </summary>
    <div class="accord-body">
        {pet_block}
    </div>
</details>

<details class="accord">
    <summary>🛏️ Room preferences</summary>
    <div class="accord-body">
        <p class="hint">Coming soon — drag your preferences (upstairs, bathtub, no carpet, balcony, etc.) into priority order. Above the line matters to you, below the line is "no preference." Not guaranteed, but we use it to match you when we assign rooms on the morning of arrival.</p>
    </div>
</details>

<details class="accord">
    <summary>❌ Cancel reservation</summary>
    <div class="accord-body">
        <p class="hint">Coming soon — view the refund policy and confirm cancellation.</p>
    </div>
</details>

<details class="accord">
    <summary>🚪 Check out</summary>
    <div class="accord-body">
        <p class="hint">Coming soon — tap when you've left so housekeeping can start.</p>
    </div>
</details>

<details class="accord">
    <summary>❓ FAQ &amp; Ask Iris</summary>
    <div class="accord-body">
        <p class="hint">Coming soon — questions about the hotel, sunsets at South Jetty, dunes at North Jetty, the Heceta lighthouse, and anywhere else worth visiting nearby.</p>
    </div>
</details>

<div class="footer">
    Need help? Call <a href="tel:{phone_tel}">{phone_display}</a>.<br/>
    Reply STOP to any of our texts to opt out.
</div>
"""


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
    publishable_key: str,
    *,
    overrides: dict[str, str] | None = None,
    error_message: str | None = None,
) -> str:
    """Render the card-on-file list + raw-card 'add card' inline form.

    `overrides` carries safe fields (cardholder name, billing zip) back
    from a failed POST so the guest doesn't have to retype them.

    `error_message` is the Cloudbeds/validation message from the prior
    attempt -- rendered inline above the form so the guest sees WHY it
    failed without scrolling back up to the banner."""
    o = overrides or {}
    name_value = (o.get("card_holder_name") or "")[:200]
    zip_value = (o.get("card_address_zip") or "")[:20]
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
            '<p class="card-info">Your booking was made through a travel '
            'site. That card covers the room rate only -- to allow '
            'incidentals (parking, late checkout, damages), please add '
            'a personal card below.</p>'
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

    # Contextual error (rendered inline above the form) so the guest sees
    # WHY their last attempt failed without scrolling back up to the banner.
    # NEVER includes card data.
    error_html = ""
    if error_message:
        error_html = (
            f'<div class="gated-prompt" style="margin:6px 0 14px;">'
            f'<strong>That card didn\'t go through.</strong> '
            f'Our payment processor said: <em>{_esc(error_message)}</em>. '
            f'Please double-check the card number, expiration date, and CVV, '
            f'then try again. If it keeps failing, call the front desk at '
            f'<a href="tel:{settings.hotel_phone_tel}">{settings.hotel_phone_display}</a> '
            f'and we\'ll sort it out.'
            f'</div>'
        )

    # Raw-card form: Cloudbeds tokenizes server-side. The form posts over
    # HTTPS to our backend, which forwards immediately to Cloudbeds'
    # postCreditCard and scrubs the local variables. autocomplete attributes
    # let the OS / password manager fill in the standard way. We don't
    # store PAN/CVV anywhere -- not in logs, not in DB, not in error pages.
    # SAFE fields (name + zip) get pre-filled on retry; sensitive fields
    # (PAN, exp, CVV) are always blank.
    raw_form = f"""
<form class="contact card" method="post" action="{action_url}" novalidate autocomplete="on">
    <label for="card_holder_name">Cardholder name</label>
    <input type="text" id="card_holder_name" name="card_holder_name"
           value="{_esc(name_value)}" autocomplete="cc-name" />

    <label for="card_number">Card number</label>
    <input type="text" id="card_number" name="card_number"
           autocomplete="cc-number" inputmode="numeric"
           pattern="[\\d\\s\\-]{{13,23}}"
           title="13-19 digit card number" required />

    <div class="row">
        <div>
            <label for="exp_month">Exp month</label>
            <input type="text" id="exp_month" name="exp_month"
                   autocomplete="cc-exp-month" inputmode="numeric"
                   maxlength="2" pattern="[01]?\\d" required />
        </div>
        <div>
            <label for="exp_year">Exp year</label>
            <input type="text" id="exp_year" name="exp_year"
                   autocomplete="cc-exp-year" inputmode="numeric"
                   maxlength="4" pattern="\\d{{2,4}}" required />
        </div>
        <div>
            <label for="card_cvv">CVV</label>
            <input type="text" id="card_cvv" name="card_cvv"
                   autocomplete="cc-csc" inputmode="numeric"
                   maxlength="4" pattern="\\d{{3,4}}" required />
        </div>
    </div>

    <label for="card_address_zip">Billing ZIP (optional)</label>
    <input type="text" id="card_address_zip" name="card_address_zip"
           value="{_esc(zip_value)}"
           autocomplete="postal-code" maxlength="10" />

    <button type="submit">Add card</button>
</form>
<p class="card-info" style="margin-top:10px; font-size:12px;">
    Card details are sent securely to our reservation system and tokenized
    immediately. We never store your card number.
</p>
"""
    return card_list_html + intro + error_html + raw_form


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
    """Form for declaring 0 / "1 or 2" / 3 dogs. Cats get a static no-go note.

    Pricing is tier-based, NOT per-dog: 1-2 dogs cost the same ($20 for up
    to a week), 3 dogs is $40. The form collapses 1 and 2 into one option
    since the financial outcome is identical. Submitted values are 0, 2,
    or 3 -- a latest row with dog_count==1 (from earlier form versions or
    a future single-count picker) is still recognized and pre-checks the
    combined option."""
    current = latest_decl.dog_count if latest_decl else 0
    # The combined radio covers 1 and 2; submit-value is 2 (upper end of
    # the bucket). 0 and 3 map straight through.
    in_small_bucket = current in (1, 2)
    def _chk(condition: bool) -> str: return "checked" if condition else ""
    return f"""
<form class="contact pet" method="post" action="{action_url}" novalidate>
    <p class="hint">Cats are not allowed at the Lighthouse Inn. If you're
        bringing a dog, please let us know so we can prepare your room.
        Pricing is for stays up to one week &mdash; if you're staying
        longer, the front desk will follow up.</p>
    <label class="pet-choice">
        <input type="radio" name="dog_count" value="0" {_chk(current == 0)} />
        <span>Just me &mdash; no pets</span>
    </label>
    <label class="pet-choice">
        <input type="radio" name="dog_count" value="2" {_chk(in_small_bucket)} />
        <span>1 or 2 dogs &nbsp;<em>($20)</em></span>
    </label>
    <label class="pet-choice">
        <input type="radio" name="dog_count" value="3" {_chk(current == 3)} />
        <span>3 dogs &nbsp;<em>($40)</em></span>
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
    """Operational gate: until both signature and incidentals card are
    confirmed, the door code stays out of the lock AND off the portal page.
    Show the guest what they need to do to unlock the code.

    For phase == future we always show the 'will be assigned morning of'
    framing -- room assignment runs that morning even after sig+CC done.

    signature_complete / card_on_file are stubs for now -- those features
    will write real state once built. Both default to False so the gated
    prompt shows up until the underlying features land."""
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
            f'Once both are complete, we\'ll send your room number and door code here.</div>'
        )

    # Gate is open. Show whatever we currently have.
    if not room_name and not door_code:
        return (
            '<p>Your room will be assigned on the morning of your arrival. '
            'Your door code will appear here as soon as it\'s set up.</p>'
        )

    parts = []
    if room_name:
        parts.append(f'<dl class="kv"><dt>Room</dt><dd><strong>{room_name}</strong></dd></dl>')
    else:
        parts.append('<p>Your room is being assigned now.</p>')
    if door_code:
        parts.append(f'<p>Door code:</p><div class="code-display">{door_code}</div>')
        parts.append('<p class="hint">Tap to copy. The code unlocks both your room and the building entry.</p>')
    else:
        parts.append('<p class="hint">Your door code will appear here once your room is ready.</p>')
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
) -> HTMLResponse:
    guest_name = (res.get("guest_name") or "").strip()
    first_name = guest_name.split(" ")[0] if guest_name else first_name_fallback
    phase = res.get("stay_phase") or "unknown"
    cb_status = res.get("status") or ""
    check_in = res.get("check_in") or ""
    check_out = res.get("check_out") or ""
    res_id_raw = res.get("reservation_id") or ""
    reservation_id_display = res_id_raw or "—"
    s = _status_for_phase(phase, cb_status, check_in, check_out)

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
    room_block = _render_room_block(
        res, signature_complete=signature_complete, card_on_file=card_on_file,
    )

    sms_opted_in = await _current_sms_opt_in(db, res_id_raw)
    contact_block = _render_contact_block(
        res, contact_action_url, sms_opted_in, overrides=contact_overrides,
    )
    contact_complete = _is_contact_section_complete(res)
    # Open the accordion when the user is being asked to re-check a field
    # (post-warn redirect) so they don't have to hunt for it. The URL fragment
    # in the redirect (#contact-section) handles the actual scroll.
    contact_open_attr = (
        "open"
        if (saved in ("contact_phone_warn", "contact_error") or contact_overrides)
        else ""
    )

    # Pet declaration: latest row drives the form pre-fill and the checkmark.
    # Section is "complete" once the guest has saved it at least once -- even
    # answering "no pets" counts as an acknowledgement.
    pet_action_url = contact_action_url.replace("/contact", "/pets")
    pet_latest = await _latest_pet_declaration(db, res_id_raw)
    pet_block = _render_pet_block(pet_latest, pet_action_url)
    pet_complete_class = "complete" if pet_latest is not None else ""
    pet_open_attr = "open" if saved in ("pets_error",) else ""

    # Signature agreement: complete = a SignatureAgreement row exists. Form
    # vs read-only summary is decided inside _render_sign_block.
    sign_action_url = contact_action_url.replace("/contact", "/sign")
    sign_block = _render_sign_block(latest_sig, sign_action_url, guest_name)
    sign_complete_class = "complete" if signature_complete else ""
    sign_open_attr = "open" if saved in ("sign_error",) else ""

    # Credit card on file: form lists existing cards + lets the guest add
    # a new one via Stripe Elements. Section is "complete" when there's a
    # non-virtual card on the reservation.
    card_action_url = contact_action_url.replace("/contact", "/cards")
    card_block = _render_card_block(
        res, card_action_url, settings.stripe_publishable_key,
        overrides=card_overrides, error_message=card_msg,
    )
    card_complete_class = "complete" if card_on_file else ""
    card_open_attr = "open" if (saved == "card_error" or card_msg) else ""

    body = _PORTAL_PAGE_BODY_TPL.format(
        first_name=first_name or "there",
        saved_banner_html=_saved_banner_html(saved),
        badge=s["badge"], badge_class=s["badge_class"],
        headline=s["headline"], body=s["body"],
        check_in=check_in or "—",
        check_out=check_out or "—",
        countdown_html=countdown_html,
        room_block=room_block,
        contact_block=contact_block,
        contact_complete_class="complete" if contact_complete else "",
        contact_open_attr=contact_open_attr,
        pet_block=pet_block,
        pet_complete_class=pet_complete_class,
        pet_open_attr=pet_open_attr,
        sign_block=sign_block,
        sign_complete_class=sign_complete_class,
        sign_open_attr=sign_open_attr,
        card_block=card_block,
        card_complete_class=card_complete_class,
        card_open_attr=card_open_attr,
        guest_name=guest_name or "—",
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
    """Build the redirect URL after a card-add POST. On failure we carry
    back the SAFE fields the guest typed (name + ZIP) so they don't have
    to re-enter them along with the rest -- and the Cloudbeds error
    message so the next page can tell the guest WHY it failed instead of
    a generic 'couldn't save'.

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
    """Forward the raw card to Cloudbeds postCreditCard. Returns
    (ok, error_message). Caller is responsible for scrubbing the inputs
    after this returns (they pass through here once)."""
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


@router.post("/g/{token}/cards")
async def post_card_by_token(
    token: str,
    request: Request,
    card_holder_name: str = Form(default=""),
    card_number: str = Form(default=""),
    exp_month: str = Form(default=""),
    exp_year: str = Form(default=""),
    card_cvv: str = Form(default=""),
    card_address_zip: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(PortalToken, token)
    if row is None or row.purpose != "guest_portal":
        return _portal_page("Not found", _NOT_FOUND_PAGE)
    if row.expires_at < datetime.utcnow():
        return _portal_page("Expired", _EXPIRED_PAGE)
    ok, err = await _apply_card_attach(
        row.reservation_id,
        card_holder_name=card_holder_name, card_number=card_number,
        exp_month=exp_month, exp_year=exp_year, card_cvv=card_cvv,
        card_address_zip=card_address_zip,
    )
    return RedirectResponse(
        url=_build_card_redirect(
            f"/g/{token}", ok, err,
            card_holder_name=card_holder_name, card_address_zip=card_address_zip,
        ),
        status_code=303,
    )


@router.post("/h{stem}/cards")
async def post_card_by_prefix(
    stem: str,
    request: Request,
    card_holder_name: str = Form(default=""),
    card_number: str = Form(default=""),
    exp_month: str = Form(default=""),
    exp_year: str = Form(default=""),
    card_cvv: str = Form(default=""),
    card_address_zip: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    prefix = _trim_to_first4_digits(stem)
    if prefix is None:
        return _portal_page("Not found", _NOT_FOUND_PAGE)
    verified_res_id = _read_verify_cookie(request.cookies.get(VERIFY_COOKIE_NAME))
    if not verified_res_id or not verified_res_id.startswith(prefix):
        log.warning("portal: card POST rejected ip=%s prefix=%s cookie=%s",
                    _client_ip(request), prefix, "yes" if verified_res_id else "no")
        return RedirectResponse(url=f"/h{prefix}", status_code=303)
    ok, err = await _apply_card_attach(
        verified_res_id,
        card_holder_name=card_holder_name, card_number=card_number,
        exp_month=exp_month, exp_year=exp_year, card_cvv=card_cvv,
        card_address_zip=card_address_zip,
    )
    return RedirectResponse(
        url=_build_card_redirect(
            f"/h{prefix}", ok, err,
            card_holder_name=card_holder_name, card_address_zip=card_address_zip,
        ),
        status_code=303,
    )
