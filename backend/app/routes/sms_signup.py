"""SMS signup webhook — POST endpoint called by the WordPress
/sms-signup/ Fluent Forms when a guest submits the form.

End-to-end flow:

  1. Guest fills out the form at
     https://lighthouseinn-florence.com/sms-signup/, checks the consent
     box, and submits.
  2. Fluent Forms POSTs JSON to https://<droplet>/sms-signup/webhook
     with header `X-Signup-Secret: <shared secret>`.
  3. This endpoint validates the secret + payload, normalizes the
     mobile number to E.164, applies abuse rate limits, dedupes against
     recent opt-ins, then inserts an sms_consent row with
     action="opt_in" and source="web_form_signup".
  4. We immediately send a one-time confirmation SMS through the
     Messaging Service ("Lighthouse Inn: you're signed up for SMS..."),
     and record the Twilio SID on the same row.

This URL IS the verifiable opt-in path we cite to Twilio for the A2P
10DLC campaign. The Twilio reviewer will load the WordPress page,
inspect the consent text, and (in some reviews) re-load it to confirm
the checkbox is required and unchecked by default. Keep the form +
this endpoint in sync.

This module also hosts /sms-signup/twilio-inbound -- the webhook Twilio
calls when a guest replies STOP / START / HELP / etc. to one of our
outbound texts. We mirror the keyword state into sms_consent so the
audit log stays consistent with Twilio's internal opt-out list, then
return empty TwiML (Twilio's campaign-level keyword config handles the
actual auto-reply text; replying again here would double-send).
"""
import base64
import hashlib
import hmac
import logging
from datetime import datetime, timedelta
from typing import Any

import phonenumbers
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.database import AsyncSessionLocal, get_db
from app.models.sms_consent import SmsConsent
from app.tools.twilio_sms import send_sms

log = logging.getLogger(__name__)
router = APIRouter()


# ──────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────

# Window for treating a duplicate (same phone + same reservation) as a
# no-op rather than a fresh consent. The guest double-tapping Submit
# shouldn't trigger two confirmation texts or two audit rows.
_DEDUPE_WINDOW = timedelta(hours=24)

# Abuse caps. A spammer scripting this endpoint against random numbers
# could rack up Twilio charges and harass real people. These are
# conservative; bump if legitimate traffic ever hits them.
_PER_PHONE_MAX_PER_DAY = 3   # opt_in rows / 24h per phone_e164
_PER_IP_MAX_PER_HOUR = 5     # opt_in rows / 1h per submitter IP

_SOURCE = "web_form_signup"

# CTIA-standard keyword sets matched (case-insensitive) against the first
# word of an inbound SMS body. The campaign UI lists the same sets in
# Twilio so their carrier-level handlers fire too -- ours just mirror the
# state into our audit log.
_STOP_KEYWORDS = {"STOP", "UNSUBSCRIBE", "QUIT", "CANCEL", "END", "OPTOUT", "REVOKE", "STOPALL"}
_START_KEYWORDS = {"START", "UNSTOP", "YES", "SUBSCRIBE"}
_HELP_KEYWORDS = {"HELP", "INFO"}

# Empty TwiML response. We send this on every inbound webhook because the
# Twilio Messaging Service's own keyword config already issues the
# carrier-required reply (STOP confirmation, HELP text, START
# acknowledgement). Adding our own <Message> on top would double-send.
_EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


# ──────────────────────────────────────────────────────────────────────────
# Request shape
# ──────────────────────────────────────────────────────────────────────────


class SignupWebhook(BaseModel):
    """Payload Fluent Forms POSTs to /sms-signup/webhook.

    Field names match the WordPress form's field names exactly. If you
    rename a field there, rename it here too -- otherwise the webhook
    422s and the guest gets a silent failure.
    """

    name: str = Field(min_length=1, max_length=200)
    # Reservation number is OPTIONAL -- some guests sign up before they have
    # their confirmation # handy, or via the front-desk flow where no number
    # exists yet. Empty string is recorded as-is; staff can match the
    # consent row to a booking later by phone or name.
    reservation_number: str = Field(default="", max_length=64)
    mobile: str = Field(min_length=7, max_length=32)

    # Fluent Forms can send a checkbox value in several shapes (bool,
    # the option label, "yes"/"on"/"1", etc.) depending on field config.
    # Accept anything here and decide truthiness in _is_truthy(). The
    # form makes the box required, so an unchecked box never submits --
    # if the request reaches us, the user did check it. This is
    # defense-in-depth, not the primary gate.
    consent: Any

    # Verbatim consent text the guest agreed to. Optional in the schema
    # but strongly recommended in practice -- store the literal disclosure
    # as evidence so later changes to the form copy don't erase the
    # meaning of historical consents.
    consent_text: str | None = Field(default=None, max_length=4000)

    # Version tag for the consent text (e.g. "v1_2026-05-25"). Bump in
    # the WP form whenever you change the disclosure copy so each row
    # unambiguously links to a known version.
    consent_text_version: str = Field(min_length=1, max_length=64)

    # Browser metadata Fluent Forms fills via its {ip} and {user_agent}
    # smart codes. Both optional; if missing we still record the consent
    # but lose the per-IP rate-limit signal for this submission.
    submitter_ip: str | None = Field(default=None, max_length=64)
    user_agent: str | None = Field(default=None, max_length=500)


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


_FALSY_STRINGS = {"", "false", "no", "0", "off", "unchecked", "none", "null"}


def _is_truthy(value: Any) -> bool:
    """True if a checkbox value indicates "the box was checked".

    Permissive on the affirmative side (Fluent Forms can send the option
    label, a bool, or a stringified flag), strict on the negative side
    (any explicit falsey marker is rejected).
    """
    if value is None or value is False:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in _FALSY_STRINGS
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return bool(value)


def _normalize_us_mobile(raw: str) -> str:
    """Parse raw user input to E.164. Raises HTTPException(400) on bad input.

    Assumes US if no explicit country code -- guests overwhelmingly enter
    "(541) 555-0123" without "+1". Reject anything libphonenumber says
    isn't a valid number (covers landlines, malformed input, etc.).
    Expand to international parsing later if we open the form up beyond
    US guests.
    """
    try:
        parsed = phonenumbers.parse(raw, "US")
    except phonenumbers.NumberParseException as e:
        raise HTTPException(status_code=400, detail=f"Mobile number unparseable: {e}")
    if not phonenumbers.is_valid_number(parsed):
        raise HTTPException(status_code=400, detail="Mobile number is not a valid US number.")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


async def _check_rate_limits(
    db: AsyncSession, *, phone_e164: str, submitter_ip: str | None
) -> None:
    """Raise HTTPException(429) if inserting one more opt_in right now
    would exceed either the per-phone or per-IP cap.

    Per-phone is always enforced. Per-IP is skipped silently when we
    have no IP (Fluent Forms not configured to send {ip}).
    """
    now = datetime.utcnow()

    phone_count_stmt = (
        select(func.count(SmsConsent.id))
        .where(SmsConsent.phone_e164 == phone_e164)
        .where(SmsConsent.action == "opt_in")
        .where(SmsConsent.recorded_at >= now - timedelta(hours=24))
    )
    phone_count = (await db.execute(phone_count_stmt)).scalar_one()
    if phone_count >= _PER_PHONE_MAX_PER_DAY:
        log.warning("SMS signup rate-limited (phone): %s count=%s", phone_e164, phone_count)
        raise HTTPException(
            status_code=429,
            detail="Too many signups for this phone today. Try again tomorrow.",
        )

    if submitter_ip:
        ip_count_stmt = (
            select(func.count(SmsConsent.id))
            .where(SmsConsent.client_ip == submitter_ip)
            .where(SmsConsent.action == "opt_in")
            .where(SmsConsent.recorded_at >= now - timedelta(hours=1))
        )
        ip_count = (await db.execute(ip_count_stmt)).scalar_one()
        if ip_count >= _PER_IP_MAX_PER_HOUR:
            log.warning("SMS signup rate-limited (ip): %s count=%s", submitter_ip, ip_count)
            raise HTTPException(
                status_code=429,
                detail="Too many signups from this address. Try again later.",
            )


async def _existing_recent_opt_in(
    db: AsyncSession, *, phone_e164: str, reservation_number: str
) -> SmsConsent | None:
    """Return the most recent opt_in row for this (phone, reservation)
    within the dedupe window, or None.
    """
    cutoff = datetime.utcnow() - _DEDUPE_WINDOW
    stmt = (
        select(SmsConsent)
        .where(SmsConsent.phone_e164 == phone_e164)
        .where(SmsConsent.reservation_id == reservation_number)
        .where(SmsConsent.action == "opt_in")
        .where(SmsConsent.recorded_at >= cutoff)
        .order_by(SmsConsent.recorded_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


# ──────────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────────


@router.get("/health")
async def signup_health():
    """Sanity check for the operator. Confirms the route is mounted and
    the shared secret is configured (without revealing the secret itself).

    Hit this once after deploy to verify the droplet picked up the new
    SMS_SIGNUP_SHARED_SECRET env var. If `secret_configured` is False,
    the /webhook endpoint will refuse every request with 503.
    """
    return {
        "status": "ok",
        "secret_configured": bool(settings.sms_signup_shared_secret),
        "origin_host_expected": settings.sms_signup_origin_host,
    }


@router.post("/webhook")
async def signup_webhook(
    payload: SignupWebhook,
    request: Request,
    background_tasks: BackgroundTasks,
    x_signup_secret: str | None = Header(default=None, alias="X-Signup-Secret"),
    db: AsyncSession = Depends(get_db),
):
    """Receive a guest's SMS opt-in from the WordPress /sms-signup/ form.

    Status codes:
      200 -- consent recorded (`status` is `opted_in` or `already_opted_in`)
      400 -- bad payload (consent not granted, malformed number, etc.)
      401 -- wrong / missing X-Signup-Secret
      429 -- rate limited (per-phone or per-IP)
      503 -- endpoint disabled (SMS_SIGNUP_SHARED_SECRET is blank in env)
    """
    # ── 1. Endpoint enabled?
    if not settings.sms_signup_shared_secret:
        log.warning(
            "SMS signup webhook called but SMS_SIGNUP_SHARED_SECRET is blank; refusing."
        )
        raise HTTPException(
            status_code=503, detail="SMS signup endpoint not configured on this droplet."
        )

    # ── 2. Authn via shared secret (constant-time compare to dodge timing leaks)
    if not x_signup_secret or not hmac.compare_digest(
        x_signup_secret, settings.sms_signup_shared_secret
    ):
        peer = request.client.host if request.client else "?"
        log.warning("SMS signup: bad/missing X-Signup-Secret from %s", peer)
        raise HTTPException(status_code=401, detail="Bad or missing X-Signup-Secret header.")

    # ── 3. Consent must actually be granted (belt-and-suspenders past required-field)
    if not _is_truthy(payload.consent):
        log.info("SMS signup: payload had falsey consent=%r -- rejecting.", payload.consent)
        raise HTTPException(status_code=400, detail="Consent checkbox not granted.")

    # ── 4. Normalize phone
    phone_e164 = _normalize_us_mobile(payload.mobile)

    # ── 5. Rate limits
    await _check_rate_limits(db, phone_e164=phone_e164, submitter_ip=payload.submitter_ip)

    # ── 6. Dedupe (same phone + reservation within the last 24h)
    existing = await _existing_recent_opt_in(
        db, phone_e164=phone_e164, reservation_number=payload.reservation_number
    )
    if existing is not None:
        log.info(
            "SMS signup dedupe-hit: phone=%s res=%s existing_id=%s",
            phone_e164,
            payload.reservation_number,
            existing.id,
        )
        return {
            "success": True,
            "status": "already_opted_in",
            "consent_id": existing.id,
        }

    # ── 7. Insert consent row
    row = SmsConsent(
        reservation_id=payload.reservation_number,
        guest_id=None,
        phone_e164=phone_e164,
        action="opt_in",
        source=_SOURCE,
        consent_text=payload.consent_text,
        consent_version=payload.consent_text_version,
        client_ip=payload.submitter_ip,
        user_agent=payload.user_agent,
        recorded_at=datetime.utcnow(),
        guest_name=payload.name.strip()[:200],
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    log.info(
        "SMS signup recorded: id=%s phone=%s res=%s name=%r",
        row.id,
        phone_e164,
        payload.reservation_number,
        payload.name,
    )

    # ── 8. Send one-time confirmation SMS through the Messaging Service
    confirmation_body = (
        f"Lighthouse Inn: you're signed up for SMS for reservation "
        f"{payload.reservation_number}. Reply STOP to opt out, HELP for help."
    )
    sms_result = await send_sms(phone_e164, confirmation_body)

    if sms_result.get("success"):
        row.confirmation_sms_sid = sms_result.get("sid")
        row.confirmation_sent_at = datetime.utcnow()
        await db.commit()
        log.info(
            "SMS signup confirmation sent: id=%s sid=%s",
            row.id,
            row.confirmation_sms_sid,
        )
    else:
        # The consent IS recorded; only the confirmation SMS failed. Most
        # likely cause early on is that the A2P 10DLC campaign isn't
        # approved yet and the Messaging Service refuses the send. Log
        # loud, don't 5xx -- the WP page should still show success and
        # staff can follow up manually for the small number of pre-approval
        # signups.
        log.warning(
            "SMS signup confirmation FAILED: id=%s phone=%s err=%s",
            row.id,
            phone_e164,
            sms_result.get("error"),
        )

    # ── 9. Background-validate the reservation number against Cloudbeds.
    # Warn-but-accept policy: we never reject a consent because the rez #
    # didn't match. The result lands on the same row's reservation_match
    # column so staff can spot typos / pre-booking signups in a periodic
    # report. Skip entirely when no number was provided.
    if payload.reservation_number:
        background_tasks.add_task(
            _validate_reservation_in_background,
            row.id,
            payload.reservation_number,
        )

    return {
        "success": True,
        "status": "opted_in",
        "consent_id": row.id,
        "confirmation_sms_sid": row.confirmation_sms_sid,
        "confirmation_error": None if sms_result.get("success") else sms_result.get("error"),
    }


# ──────────────────────────────────────────────────────────────────────────
# Background tasks
# ──────────────────────────────────────────────────────────────────────────


async def _validate_reservation_in_background(
    consent_id: int, reservation_number: str
) -> None:
    """Look up the reservation in Cloudbeds and stamp the result on the
    sms_consent row. Runs after signup_webhook has already returned 200
    to the WP snippet, so it adds zero latency to the user-visible flow.

    Never raises -- Cloudbeds outages must not strand consent records.
    """
    # Imported lazily so a missing/broken cloudbeds module can't take down
    # the signup webhook's import chain at startup.
    from app.tools.cloudbeds import get_reservation_by_id

    try:
        result = await get_reservation_by_id(reservation_number)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "SMS signup bg-validate: Cloudbeds lookup crashed id=%s res=%s err=%s",
            consent_id, reservation_number, e,
        )
        return

    matched = result is not None

    async with AsyncSessionLocal() as db:
        row = await db.get(SmsConsent, consent_id)
        if row is None:
            log.warning(
                "SMS signup bg-validate: consent row %s vanished before stamping",
                consent_id,
            )
            return
        row.reservation_match = 1 if matched else 0
        await db.commit()

    if matched:
        log.info(
            "SMS signup bg-validate: reservation MATCHED id=%s res=%s guest=%r",
            consent_id, reservation_number, result.get("guest_name"),
        )
    else:
        log.warning(
            "SMS signup bg-validate: reservation NOT FOUND in Cloudbeds id=%s res=%s "
            "(consent still valid; staff should follow up)",
            consent_id, reservation_number,
        )


# ──────────────────────────────────────────────────────────────────────────
# Twilio inbound SMS webhook (STOP / START / HELP keyword mirroring)
# ──────────────────────────────────────────────────────────────────────────


def _verify_twilio_signature(
    request_url: str, form_params: dict[str, Any], signature_header: str
) -> bool:
    """Verify Twilio's X-Twilio-Signature header.

    Twilio signs: HMAC-SHA1(URL + concat(sorted_param_pairs), AuthToken),
    base64-encoded. Failing the check means someone other than Twilio
    POSTed to our endpoint -- treat as forgery and refuse.
    """
    if not signature_header or not settings.twilio_auth_token:
        return False
    sorted_pairs = sorted(form_params.items())
    payload = request_url + "".join(f"{k}{v}" for k, v in sorted_pairs)
    computed = base64.b64encode(
        hmac.new(
            settings.twilio_auth_token.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode()
    return hmac.compare_digest(computed, signature_header)


async def _record_consent_change(
    db: AsyncSession,
    *,
    phone_e164: str,
    action: str,
    source: str,
    message_sid: str,
) -> int:
    """Insert a new sms_consent row capturing an opt-in/opt-out event
    triggered by an inbound SMS keyword. Returns the new row's id.

    We deliberately store one row per event (never UPDATE) so the audit
    log retains the full history -- TCPA disputes turn on "did you have
    proof of consent on date X", which requires immutable rows.
    """
    row = SmsConsent(
        reservation_id="",  # SMS-keyword events have no reservation context
        guest_id=None,
        phone_e164=phone_e164,
        action=action,
        source=source,
        consent_text=None,
        consent_version=None,
        client_ip=None,
        user_agent=None,
        recorded_at=datetime.utcnow(),
        guest_name=None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    log.info(
        "SMS consent change recorded (inbound): id=%s phone=%s action=%s source=%s sid=%s",
        row.id, phone_e164, action, source, message_sid,
    )
    return row.id


@router.post("/twilio-inbound")
async def twilio_sms_inbound(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Twilio inbound SMS webhook. Twilio POSTs here for every inbound
    message to our Messaging Service numbers. We mirror STOP / START
    keyword state into sms_consent so our audit log stays consistent
    with Twilio's internal opt-out list.

    Twilio's campaign-level keyword config handles the actual reply
    text (the STOP confirmation, the HELP text, etc.) -- we return
    empty TwiML so we don't double-send. Adding a <Message> on top of
    Twilio's auto-reply would deliver two messages to the guest.

    Auth: Twilio signs every webhook with HMAC-SHA1 in X-Twilio-Signature.
    We verify before touching the database -- otherwise anyone could
    spoof STOPs to opt out other people's numbers.
    """
    form_data = await request.form()
    # Convert to a plain dict for signature verification (Twilio expects
    # all values as strings).
    form_params: dict[str, Any] = {k: str(v) for k, v in form_data.items()}

    signature = request.headers.get("X-Twilio-Signature", "")
    request_url = settings.twilio_inbound_base_url.rstrip("/") + str(request.url.path)
    if not _verify_twilio_signature(request_url, form_params, signature):
        peer = request.client.host if request.client else "?"
        log.warning(
            "Twilio inbound: bad/missing signature from %s url=%s (refusing)",
            peer, request_url,
        )
        return Response(status_code=403, content="Forbidden")

    body_text = form_params.get("Body", "").strip()
    from_number = form_params.get("From", "").strip()
    to_number = form_params.get("To", "").strip()
    message_sid = form_params.get("MessageSid", "")

    log.info(
        "Twilio inbound SMS: from=%s to=%s sid=%s body=%r",
        from_number, to_number, message_sid, body_text[:200],
    )

    if not from_number:
        # No From -- nothing to mirror. Acknowledge and bail.
        return Response(content=_EMPTY_TWIML, media_type="application/xml")

    # Normalize From to E.164. Twilio guarantees E.164 already but be
    # defensive in case future channels (WhatsApp, etc.) reuse this hook.
    try:
        parsed = phonenumbers.parse(from_number, "US")
        phone_e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        phone_e164 = from_number  # fall back to whatever Twilio sent

    first_word = body_text.split(maxsplit=1)[0].upper() if body_text else ""

    if first_word in _STOP_KEYWORDS:
        await _record_consent_change(
            db,
            phone_e164=phone_e164,
            action="opt_out",
            source="sms_reply_stop",
            message_sid=message_sid,
        )
    elif first_word in _START_KEYWORDS:
        await _record_consent_change(
            db,
            phone_e164=phone_e164,
            action="opt_in",
            source="sms_reply_start",
            message_sid=message_sid,
        )
    elif first_word in _HELP_KEYWORDS:
        # No DB change for HELP -- it's a passive query, not a state change.
        # Twilio's campaign-level HELP keyword handles the reply text.
        log.info(
            "Twilio inbound: HELP from %s (Twilio campaign auto-replies)",
            phone_e164,
        )
    else:
        # Non-keyword reply (a guest texting back conversationally to a
        # transactional message). Log so staff can spot real questions in
        # the journal, but don't pretend it's a consent change.
        log.info(
            "Twilio inbound: non-keyword reply from %s body=%r (logged, no action)",
            phone_e164, body_text[:100],
        )

    return Response(content=_EMPTY_TWIML, media_type="application/xml")
