"""Guest portal — Add-Card flow. [DEPRECATED — see /g/{token} flow]

DEPRECATION NOTE (2026-05-27): The add-card UX is now unified inside the
main guest portal at /g/{token}. The card accordion in that page renders
the same Stripe Elements form this file used to expose at
/portal-card/{token}, and the new entry point for SMS-delivered card
links is /g/{token}?open=card (which auto-expands the card accordion on
arrival).

This file is kept around in case stale Twilio retries deliver an old
/portal-card/{token} URL, but no new code paths point here. To finish
the migration:
  1. Confirm no live SMS messages still reference /portal-card URLs
     (Twilio message log + portal_token rows with purpose='add_card').
  2. Delete this file + drop the /portal-card/* routes from main.py.
  3. Drop or migrate any portal_token rows where purpose='add_card'.

Iris's send_card_link_via_sms tool was updated to call
issue_guest_token_row in portal.py and SMS the /g/{token}?open=card
URL instead of minting a one-shot add_card token here.

Original end-to-end flow (still works for compat):

  1. DCS / Iris calls POST /portal-card/mint with X-Portal-Auth, passing
     the reservation_id + room context. We mint a one-shot token and
     return the portal URL to SMS to the guest.
  2. Guest opens the URL (GET /portal-card/{token}). We validate the
     token, render an HTML form with Stripe Elements pre-mounted using
     Cloudbeds' platform publishable key.
  3. Guest types card, clicks submit. JS calls
     stripe.createToken(card) → POSTs the legacy token + card metadata
     to /portal-card/{token}/save.
  4. Backend resolves reservation_id → booking_id (~500ms via the
     internal search endpoint), then calls dashboard_save_credit_card
     (~1-2s via the internal save endpoint).
  5. On success: mark the portal_token row confirmed_at (one-shot done),
     return JSON. Portal shows confirmation.

Test endpoints (`/portal-card/test-add/{res_id}` + `/portal-card/test-save`)
are kept for local-dev validation without minting a token. Remove them
once we're confident the prod flow is stable.
"""
import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.database import get_db
from app.models.portal_token import PortalToken
from app.tools.cloudbeds_dashboard import (
    dashboard_save_credit_card,
    get_booking_id,
)

log = logging.getLogger(__name__)
router = APIRouter()

_PURPOSE = "add_card"
_TOKEN_TTL = timedelta(hours=24)

# Cloudbeds' platform publishable key from the dashboard's iframe src.
# Public by design — fine to embed in HTML pages we serve.
_CLOUDBEDS_STRIPE_PK = (
    "pk_live_51GxYvfCkb5UaC5yLKjotmnTBp7MYbmiTqeNvDluaevZJ7xSsbL7RC4f3ZQdglMa9IVY6iPkpfDCdSJGrgdiyvuRo00jZpsTHkv"
)


# ──────────────────────────────────────────────────────────────────────────
# Request / response shapes
# ──────────────────────────────────────────────────────────────────────────


class CardSaveTestRequest(BaseModel):
    """For the unauthenticated /portal-card/test-save endpoint."""
    reservation_id: str = Field(min_length=1)
    token_id: str = Field(min_length=1, description="Stripe legacy token id (tok_xxx)")
    token_card: dict[str, Any]


class CardSaveTokenRequest(BaseModel):
    """For the token-authenticated /portal-card/{token}/save endpoint.
    reservation_id NOT included — server reads it from the token row."""
    token_id: str = Field(min_length=1, description="Stripe legacy token id (tok_xxx)")
    token_card: dict[str, Any]


class MintRequest(BaseModel):
    """DCS-facing payload to mint a one-shot Add-Card URL."""
    reservation_id: str = Field(min_length=1, description="Public Cloudbeds reservation ID")
    first_name: str | None = None
    room_number: str = Field(min_length=1, description="Room/unit number for portal display")


class MintResponse(BaseModel):
    token: str
    portal_url: str
    expires_at: datetime


# ──────────────────────────────────────────────────────────────────────────
# Auth + token helpers
# ──────────────────────────────────────────────────────────────────────────


async def _require_portal_auth(x_portal_auth: Annotated[str | None, Header()] = None):
    """Same X-Portal-Auth pattern as portal.py. Refuse if the shared
    secret isn't configured or the header doesn't match."""
    if not settings.portal_shared_secret:
        log.warning("portal-card: PORTAL_SHARED_SECRET not configured — refusing")
        raise HTTPException(status_code=503, detail="Portal not configured.")
    if not x_portal_auth or not secrets.compare_digest(
        x_portal_auth, settings.portal_shared_secret
    ):
        raise HTTPException(status_code=401, detail="Invalid X-Portal-Auth.")


async def _resolve_token(token: str, db: AsyncSession) -> PortalToken:
    """Look up an add_card token. Raises HTTPException for not-found,
    expired, already-used, or wrong-purpose. The exception types use
    standard codes so the GET endpoint can render a friendly HTML page
    and the POST endpoint returns a clean JSON 4xx."""
    result = await db.execute(select(PortalToken).where(PortalToken.token == token))
    row = result.scalar_one_or_none()
    if row is None or row.purpose != _PURPOSE:
        raise HTTPException(status_code=404, detail="Link not found.")
    if row.expires_at < datetime.utcnow():
        raise HTTPException(status_code=403, detail="This link has expired.")
    if row.confirmed_at is not None:
        raise HTTPException(status_code=403, detail="This link has already been used.")
    return row


# ──────────────────────────────────────────────────────────────────────────
# DCS-facing: mint a token
# ──────────────────────────────────────────────────────────────────────────


async def mint_card_capture_token_row(
    db: AsyncSession,
    *,
    reservation_id: str,
    first_name: str | None,
    room_number: str,
) -> tuple[str, str, datetime]:
    """In-process helper for callers that already have a DB session and
    want to mint a card-capture token without going through the HTTP
    endpoint. Used by the Iris voice tool. Returns (token, portal_url,
    expires_at). Mirrors the mint endpoint's logic exactly so behavior
    stays consistent whether the mint comes from DCS, Iris voice, or
    manual /portal-card/mint calls."""
    token = secrets.token_urlsafe(16)
    now = datetime.utcnow()
    expires_at = now + _TOKEN_TTL
    row = PortalToken(
        token=token,
        purpose=_PURPOSE,
        reservation_id=reservation_id,
        first_name=first_name,
        room_number=room_number,
        created_at=now,
        expires_at=expires_at,
    )
    db.add(row)
    await db.commit()
    base = settings.portal_public_base_url.rstrip("/")
    portal_url = f"{base}/portal-card/{token}"
    log.info(
        "Minted add_card token=%s reservation=%s room=%s expires=%s",
        token, reservation_id, room_number, expires_at.isoformat(),
    )
    return token, portal_url, expires_at


@router.post(
    "/portal-card/mint",
    response_model=MintResponse,
    tags=["portal-card"],
    dependencies=[Depends(_require_portal_auth)],
)
async def mint_card_capture_token(
    req: MintRequest,
    db: AsyncSession = Depends(get_db),
):
    """Mint a one-shot Add-Card URL. DCS/Iris calls this with a
    reservation_id + room context, then SMSes the returned `portal_url`
    to the guest's phone. Token expires after 24h or once used,
    whichever comes first.

    This endpoint deliberately does NOT pre-resolve booking_id — the
    save endpoint does that resolution itself (it's fast: ~500ms via
    the internal search endpoint once the httpx client is warm).
    Avoids tying mint to a slower path; if the search endpoint is
    flaky at mint time we can still mint successfully."""
    token, portal_url, expires_at = await mint_card_capture_token_row(
        db,
        reservation_id=req.reservation_id,
        first_name=req.first_name,
        room_number=req.room_number,
    )
    return MintResponse(token=token, portal_url=portal_url, expires_at=expires_at)


# ──────────────────────────────────────────────────────────────────────────
# Guest-facing: token-authenticated GET (form) + POST (save)
# ──────────────────────────────────────────────────────────────────────────


@router.get(
    "/portal-card/{token}",
    response_class=HTMLResponse,
    tags=["portal-card"],
)
async def card_add_form(token: str, db: AsyncSession = Depends(get_db)):
    """Guest-facing: render the Add Card form. Token-authenticated.

    Bad-token failures render a friendly HTML message instead of the
    bare 4xx JSON FastAPI would default to — the guest is on their
    phone clicking a link, not making an API call."""
    try:
        row = await _resolve_token(token, db)
    except HTTPException as e:
        return HTMLResponse(
            _render_error_page(e.detail or "Link is no longer valid."),
            status_code=e.status_code,
        )
    html = _render_card_form(
        post_url=f"/portal-card/{token}/save",
        first_name=row.first_name,
        room_number=row.room_number,
        # JS doesn't need reservation_id when posting via the token
        # endpoint — server reads it from the token row.
        include_reservation_in_post=False,
        reservation_id=None,
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, no-store"})


@router.post("/portal-card/{token}/save", tags=["portal-card"])
async def card_save(
    token: str,
    body: CardSaveTokenRequest,
    db: AsyncSession = Depends(get_db),
):
    """Guest-facing: handle the form submission. One-shot — on success
    we set confirmed_at so the token can't be replayed (e.g. browser
    back-button + resubmit). Also relevant because Stripe legacy tokens
    are themselves single-use — even without our gate, Stripe would
    reject a retry."""
    row = await _resolve_token(token, db)

    booking_id = await get_booking_id(row.reservation_id)
    if not booking_id:
        log.warning(
            "portal-card save: couldn't resolve booking_id for reservation=%s (token=%s)",
            row.reservation_id, token,
        )
        return JSONResponse(
            {"success": False, "error": "We couldn't find the reservation. Please contact the front desk."},
            status_code=400,
        )

    log.info(
        "portal-card save: reservation=%s booking_id=%s token=%s stripe_token=%s last4=%s",
        row.reservation_id, booking_id, token,
        body.token_id, body.token_card.get("last4"),
    )

    result = await dashboard_save_credit_card(
        booking_id=booking_id,
        legacy_token_id=body.token_id,
        token_card=body.token_card,
    )

    if result.get("success"):
        row.confirmed_at = datetime.utcnow()
        await db.commit()
        log.info(
            "portal-card save: success card_id=%s for booking=%s (token=%s)",
            result.get("card_id"), booking_id, token,
        )
        return JSONResponse({
            "success": True,
            "card_id": result.get("card_id"),
            "last4": (result.get("card_details") or {}).get("card_number"),
        })

    log.warning(
        "portal-card save: dashboard_save_credit_card rejected: %s",
        json.dumps(result, default=str)[:500],
    )
    # Surface a clean guest-friendly error; the server log has details.
    return JSONResponse({
        "success": False,
        "error": "We couldn't save your card. Please try again, or contact the front desk.",
    }, status_code=400)


# ──────────────────────────────────────────────────────────────────────────
# Test endpoints (unauthenticated; for local dev validation only)
# ──────────────────────────────────────────────────────────────────────────


@router.get(
    "/portal-card/test-add/{reservation_id}",
    response_class=HTMLResponse,
    tags=["portal-card"],
)
async def card_add_form_test(reservation_id: str):
    """TEST-ONLY: render the form for a reservation, no auth. Lets us
    validate the Stripe.js → save flow without the token machinery."""
    html = _render_card_form(
        post_url="/portal-card/test-save",
        first_name=None,
        room_number="(test)",
        include_reservation_in_post=True,
        reservation_id=reservation_id,
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, no-store"})


@router.post("/portal-card/test-save", tags=["portal-card"])
async def card_save_test(req: CardSaveTestRequest):
    """TEST-ONLY counterpart to card_add_form_test."""
    log.info(
        "portal-card test-save: reservation=%s token=%s last4=%s",
        req.reservation_id, req.token_id, req.token_card.get("last4"),
    )
    booking_id = await get_booking_id(req.reservation_id)
    if not booking_id:
        return JSONResponse(
            {"success": False, "error": "Reservation not found."}, status_code=400,
        )
    result = await dashboard_save_credit_card(
        booking_id=booking_id,
        legacy_token_id=req.token_id,
        token_card=req.token_card,
    )
    if result.get("success"):
        log.info("portal-card test-save: success card_id=%s for booking=%s",
                 result.get("card_id"), booking_id)
        return JSONResponse({
            "success": True,
            "card_id": result.get("card_id"),
            "last4": (result.get("card_details") or {}).get("card_number"),
        })
    log.warning(
        "portal-card test-save: dashboard rejected: %s",
        json.dumps(result, default=str)[:500],
    )
    return JSONResponse({
        "success": False,
        "error": result.get("error") or "Couldn't save the card.",
    }, status_code=400)


# ──────────────────────────────────────────────────────────────────────────
# HTML rendering
# ──────────────────────────────────────────────────────────────────────────


def _render_card_form(
    *,
    post_url: str,
    first_name: str | None,
    room_number: str | None,
    include_reservation_in_post: bool,
    reservation_id: str | None,
) -> str:
    """Inline HTML for the Add Card form. Two callers: the test endpoint
    (which embeds reservation_id in the POST body) and the token
    endpoint (which doesn't — server reads it from the token row).

    JSON-encode all data interpolated into JS strings so unexpected
    chars can't break out of string literals."""
    pk_js = json.dumps(_CLOUDBEDS_STRIPE_PK)
    post_url_js = json.dumps(post_url)
    reservation_id_js = json.dumps(reservation_id) if reservation_id else "null"
    include_res = "true" if include_reservation_in_post else "false"

    greeting = (
        f"Hi {first_name},"
        if first_name and first_name.strip()
        else "Hello,"
    )
    room_blurb = (
        f"Add a credit card to room {room_number}."
        if room_number and room_number != "(test)"
        else "Add a credit card to your reservation."
    )
    hotel_name = settings.hotel_name

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>{hotel_name} — Add Card</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://js.stripe.com/v3/"></script>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         max-width: 26rem; margin: 1.5rem auto; padding: 1rem; color: #222; }}
  h1 {{ font-size: 1.35rem; margin-bottom: .25rem; }}
  .greeting {{ color: #444; margin-bottom: .1rem; }}
  .subtle {{ color: #666; font-size: .9rem; margin-bottom: 1.5rem; }}
  .row {{ margin: 1rem 0; }}
  label {{ display: block; margin-bottom: .4rem; font-weight: 600; font-size: .92rem; }}
  input.txt, #card-element {{
    width: 100%; padding: .65rem .75rem; box-sizing: border-box;
    border: 1px solid #c5c5c5; border-radius: .35rem; background: white;
    font-family: inherit; font-size: 1rem;
  }}
  #card-element {{ padding: .85rem .75rem; }}
  button#submit-btn {{
    width: 100%; padding: .9rem 1rem; font-size: 1.05rem; font-weight: 600;
    background: #2b6fb6; color: white; border: 0; border-radius: .4rem;
    cursor: pointer; margin-top: 1.2rem;
  }}
  button:disabled {{ background: #888; cursor: not-allowed; }}
  .msg {{ margin-top: 1rem; padding: .8rem 1rem; border-radius: .35rem; }}
  .msg.ok {{ background: #e6f7e6; color: #0a5a20; border: 1px solid #b0e0b0; }}
  .msg.err {{ background: #fbeaea; color: #8a1414; border: 1px solid #e8b0b0; }}
  small.hint {{ color: #888; }}
</style>
</head><body>
<h1>{hotel_name}</h1>
<p class="greeting">{greeting}</p>
<p class="subtle">{room_blurb}</p>

<form id="card-form" novalidate>
  <div class="row">
    <label for="cardholder-name">Cardholder name</label>
    <input type="text" id="cardholder-name" class="txt"
           autocomplete="cc-name" required placeholder="As shown on the card" />
  </div>
  <div class="row">
    <label for="card-element">Card details</label>
    <div id="card-element"></div>
    <small class="hint">Card number · expiration · CVC. Postal code if your card uses one.</small>
    <div id="card-errors" class="msg err" role="alert" style="display:none; margin-top:.6rem"></div>
  </div>
  <button id="submit-btn" type="submit">Add card to reservation</button>
</form>

<div id="result" style="display:none" class="msg"></div>

<script>
(function() {{
  const PK = {pk_js};
  const POST_URL = {post_url_js};
  const RESERVATION_ID = {reservation_id_js};
  const INCLUDE_RES = {include_res};

  const stripe = Stripe(PK);
  const elements = stripe.elements();
  const card = elements.create('card', {{
    style: {{
      base: {{
        fontFamily: '"Segoe UI", system-ui, sans-serif',
        fontSize: '15px',
        color: '#222',
        '::placeholder': {{ color: '#888' }},
      }},
    }},
  }});
  card.mount('#card-element');

  const errEl = document.getElementById('card-errors');
  card.on('change', (event) => {{
    if (event.error) {{
      errEl.textContent = event.error.message;
      errEl.style.display = '';
    }} else {{
      errEl.style.display = 'none';
    }}
  }});

  const form = document.getElementById('card-form');
  const btn = document.getElementById('submit-btn');
  const resultEl = document.getElementById('result');

  function showResult(text, isOk) {{
    resultEl.textContent = text;
    resultEl.className = 'msg ' + (isOk ? 'ok' : 'err');
    resultEl.style.display = '';
  }}

  form.addEventListener('submit', async (e) => {{
    e.preventDefault();
    const name = document.getElementById('cardholder-name').value.trim();
    if (!name) {{
      errEl.textContent = 'Please enter the cardholder name.';
      errEl.style.display = '';
      return;
    }}
    btn.disabled = true;
    btn.textContent = 'Adding card…';
    resultEl.style.display = 'none';

    try {{
      const {{ token, error }} = await stripe.createToken(card, {{ name }});
      if (error) {{
        showResult(error.message || 'Could not tokenize the card.', false);
        return;
      }}
      const payload = {{
        token_id: token.id,
        token_card: token.card,
      }};
      if (INCLUDE_RES) payload.reservation_id = RESERVATION_ID;

      const resp = await fetch(POST_URL, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload),
      }});
      const data = await resp.json();
      if (resp.ok && data.success) {{
        showResult('Card added successfully. Last 4: ' + (data.last4 || '?'), true);
        form.style.display = 'none';
      }} else {{
        // For 4xx/5xx FastAPI returns {{detail: "..."}}; for our handlers
        // we always include `error`. Try both.
        const msg = data.error || data.detail || 'Unable to save the card.';
        showResult(msg, false);
      }}
    }} catch (ex) {{
      showResult('Network error: ' + (ex && ex.message ? ex.message : ex), false);
    }} finally {{
      btn.disabled = false;
      btn.textContent = 'Add card to reservation';
    }}
  }});
}})();
</script>
</body></html>"""


def _render_error_page(message: str) -> str:
    """Minimal HTML for a non-recoverable failure (expired token, etc).
    No form, no JS — just a friendly explanation."""
    hotel_name = settings.hotel_name
    safe_msg = message.replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>{hotel_name} — Link not available</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 26rem; margin: 2rem auto; padding: 1rem; color: #222; }}
  h1 {{ font-size: 1.35rem; }}
  .msg {{ margin-top: 1rem; padding: .8rem 1rem; border-radius: .35rem;
         background: #fbeaea; color: #8a1414; border: 1px solid #e8b0b0; }}
  .hint {{ margin-top: 1.2rem; color: #555; font-size: .9rem; }}
</style>
</head><body>
<h1>{hotel_name}</h1>
<div class="msg">{safe_msg}</div>
<p class="hint">If you need help, please contact the front desk.</p>
</body></html>"""
