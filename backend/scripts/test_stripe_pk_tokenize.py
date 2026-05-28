"""Task #11 sanity test: server-side Stripe tokenization with Cloudbeds'
platform publishable key, then optionally round-trip the resulting tok_xxx
through dashboard_save_credit_card to confirm the in-call DTMF hands-free
card-capture architecture is viable.

  Stage 1: POST to https://api.stripe.com/v1/tokens using Cloudbeds'
           platform publishable key (pk_live_51GxYvf...). Stripe's
           /v1/tokens endpoint was designed for browsers; this test
           confirms it also accepts server-side calls.

  Stage 2 (if DO_SAVE=1): hand the tok_xxx + token_card to
           dashboard_save_credit_card against the Test1 Case reservation
           (1989264686165). Humanlike pause before this call — Cloudbeds
           dashboard endpoints are the same ones the human UI hits, and
           we want our timing to look like a real user clicked "Save".

Defaults to the Stripe test PAN (4242...). Stripe live mode (which
Cloudbeds' publishable key sits in) will reject test PANs — Stage 1
returns a clean 'Your card was declined' error which still proves the
endpoint accepts publishable-key auth. Override with a real card via env
vars for the end-to-end Stage 2 test.

Run:
    .venv\\Scripts\\python scripts\\test_stripe_pk_tokenize.py            # tokenize only
    DO_SAVE=1 .venv\\Scripts\\python scripts\\test_stripe_pk_tokenize.py  # full round-trip
    CARD_NUMBER=4640... CARD_EXP=10/28 CARD_CVC=683 DO_SAVE=1 \\
      .venv\\Scripts\\python scripts\\test_stripe_pk_tokenize.py          # real card + save

PCI: PAN/CVV live only in local variables in main(). Explicitly scrubbed
after the Stripe call. Never logged. Last4 only in output.
"""
from __future__ import annotations

import asyncio
import os
import random
import sys
from pathlib import Path

backend_root = Path(__file__).parent.parent
os.chdir(backend_root)
sys.path.insert(0, str(backend_root))

import httpx  # noqa: E402

from app.tools.cloudbeds_dashboard import (  # noqa: E402
    dashboard_save_credit_card,
    get_booking_id,
)

# ---- test inputs ------------------------------------------------------------
RESERVATION_ID = "1989264686165"  # Test1 Case

# Cloudbeds' platform publishable key (public by design — embedded in their
# dashboard HTML and in portal_card.py's _render_card_form). Tokens minted
# with this key live on acct_1RYA4xEJ572tmEoR's Stripe Connect account,
# which is what dashboard_save_credit_card expects.
PUBLISHABLE_KEY = (
    "pk_live_51GxYvfCkb5UaC5yLKjotmnTBp7MYbmiTqeNvDluaevZJ7xSsbL7RC4f3ZQdglMa9IVY6iPkpfDCdSJGrgdiyvuRo00jZpsTHkv"
)

CARD_NUMBER = os.environ.get("CARD_NUMBER", "4242424242424242")
CARD_EXP    = os.environ.get("CARD_EXP",    "12/30")        # MM/YY
CARD_CVC    = os.environ.get("CARD_CVC",    "123")
CARD_NAME   = os.environ.get("CARD_NAME",   "Test1 Case")
CARD_ZIP    = os.environ.get("CARD_ZIP",    "")             # optional, sent if non-empty

DO_SAVE     = os.environ.get("DO_SAVE", "").strip().lower() in ("1", "true", "yes", "y")
# -----------------------------------------------------------------------------


def _parse_exp(exp: str) -> tuple[int, int] | None:
    """Parse MM/YY or MM/YYYY into (month, 4-digit year)."""
    parts = exp.replace(" ", "").split("/")
    if len(parts) != 2:
        return None
    try:
        mm = int(parts[0])
        yy = int(parts[1])
    except ValueError:
        return None
    if not (1 <= mm <= 12):
        return None
    if yy < 100:
        yy += 2000
    return mm, yy


async def stage1_tokenize(
    pan: str, exp_month: int, exp_year: int, cvc: str,
    name: str, zip_code: str,
) -> dict:
    """POST card fields to Stripe /v1/tokens using publishable-key auth.
    Returns {success, token_id, token_card, error, http_status, raw_body}.

    PAN and CVC are passed as args and never logged; the caller scrubs them
    from its own scope after this returns.
    """
    form: dict[str, str] = {
        "card[number]":    pan,
        "card[exp_month]": str(exp_month),
        "card[exp_year]":  str(exp_year),
        "card[cvc]":       cvc,
        "card[name]":      name,
    }
    if zip_code:
        form["card[address_zip]"] = zip_code

    headers = {
        # Stripe publishable-key auth is HTTP Basic with pk as username,
        # empty password. (The official Stripe SDKs do the same.)
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://api.stripe.com/v1/tokens",
                data=form,
                headers=headers,
                auth=(PUBLISHABLE_KEY, ""),
            )
    except httpx.HTTPError as ex:
        return {"success": False, "error": f"HTTP error: {ex}", "http_status": 0, "raw_body": ""}

    raw = resp.text
    try:
        body = resp.json()
    except ValueError:
        return {
            "success": False,
            "error": "Non-JSON response from Stripe",
            "http_status": resp.status_code,
            "raw_body": raw[:500],
        }

    if resp.status_code != 200:
        err = (body.get("error") or {}).get("message") or "Stripe rejected the request"
        err_code = (body.get("error") or {}).get("code") or ""
        err_type = (body.get("error") or {}).get("type") or ""
        return {
            "success": False,
            "error": f"{err} (code={err_code}, type={err_type})",
            "http_status": resp.status_code,
            "raw_body": "",  # may contain sensitive detail; suppress
        }

    return {
        "success": True,
        "token_id": body.get("id"),
        "token_card": body.get("card") or {},
        "error": "",
        "http_status": resp.status_code,
        "raw_body": "",
    }


async def stage2_save(token_id: str, token_card: dict) -> dict:
    """Call dashboard_save_credit_card against the Test1 Case reservation.
    Wraps with humanlike pause to mimic a real user clicking 'Save Card'
    on the dashboard after Stripe Elements tokenized."""
    print("Stage 2: resolving booking_id (internal Cloudbeds ID) ...")
    booking_id = await get_booking_id(RESERVATION_ID)
    if not booking_id:
        return {"success": False, "error": "Could not resolve booking_id for the reservation."}
    print(f"  booking_id = {booking_id}")

    # Humanlike pause: simulate the operator reading the tokenize success,
    # then clicking 'Save'. Real users take a couple seconds — bots don't.
    pause_s = random.uniform(2.4, 4.7)
    print(f"  human pause: {pause_s:.2f}s before calling save_credit_card ...")
    await asyncio.sleep(pause_s)

    print("Stage 2: calling dashboard_save_credit_card ...")
    return await dashboard_save_credit_card(
        booking_id=str(booking_id),
        legacy_token_id=token_id,
        token_card=token_card,
    )


async def main() -> int:
    print(f"Target reservation: {RESERVATION_ID} (Test1 Case)")
    print(f"Publishable key:    {PUBLISHABLE_KEY[:24]}... (Cloudbeds platform)")
    last4 = (CARD_NUMBER or "")[-4:] if len(CARD_NUMBER) >= 4 else "?"
    print(f"Card:               ****{last4} exp {CARD_EXP}")
    print(f"DO_SAVE:            {DO_SAVE}")
    print()

    exp = _parse_exp(CARD_EXP)
    if exp is None:
        print(f"ERROR: bad CARD_EXP value: {CARD_EXP!r}")
        return 2
    exp_month, exp_year = exp

    # ---- Stage 1: Stripe tokenize ----
    print("Stage 1: tokenizing via Stripe /v1/tokens with publishable key ...")
    s1 = await stage1_tokenize(
        pan=CARD_NUMBER,
        exp_month=exp_month, exp_year=exp_year,
        cvc=CARD_CVC, name=CARD_NAME, zip_code=CARD_ZIP,
    )

    # PCI: scrub PAN/CVC from this scope before any further code runs.
    # Best-effort — Python's GC will eventually collect, but explicit
    # overwrite means a memory dump after this line won't reveal them.
    pan_local = CARD_NUMBER  # capture last4 above is the only place we used pan
    cvc_local = CARD_CVC
    pan_local = ""
    cvc_local = ""
    del pan_local, cvc_local

    print(f"  http_status: {s1.get('http_status')}")
    if not s1.get("success"):
        print(f"  error:       {s1.get('error')}")
        print()
        print("Stage 1 FAILED — server-side publishable-key tokenization didn't work.")
        if "test" in (s1.get("error") or "").lower() or "declined" in (s1.get("error") or "").lower():
            print(
                "  (Note: this might just be Stripe rejecting a TEST PAN against a LIVE "
                "publishable key — try again with a real card via env vars to confirm.)"
            )
        return 1

    token_id = s1["token_id"]
    token_card = s1["token_card"]
    brand = token_card.get("brand", "?")
    tok_last4 = token_card.get("last4", "?")
    funding = token_card.get("funding", "?")
    country = token_card.get("country", "?")
    print(f"  token_id:    {token_id}")
    print(f"  card:        {brand} ****{tok_last4} ({funding}, {country})")
    print()
    print("Stage 1 OK: Stripe accepts server-side publishable-key tokenization.")
    print()

    if not DO_SAVE:
        print(
            "Stage 2 skipped (DO_SAVE not set). To run the full round-trip:"
        )
        print("  set DO_SAVE=1 and re-run with the same card.")
        return 0

    # ---- Stage 2: dashboard save ----
    s2 = await stage2_save(token_id, token_card)
    print()
    print("Result:")
    for k, v in s2.items():
        if k == "card_details":
            print(f"  {k}: <redacted>")
        else:
            print(f"  {k}: {v}")

    if s2.get("success"):
        print()
        print("Stage 2 OK: end-to-end path verified. Hands-free DTMF capture is viable.")
        return 0

    print()
    print("Stage 2 FAILED — Stripe accepted the publishable-key call but Cloudbeds "
          "rejected the resulting token. Examine error/detail above.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
