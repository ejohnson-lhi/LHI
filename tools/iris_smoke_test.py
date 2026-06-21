"""Iris synthetic-call smoke test.

Places a Twilio call to the dev DID Iris answers on, polls until it
completes, and exits 0 if Iris stayed on the line long enough to count
as "answered" — currently >= 5 seconds (her greeting is ~3 seconds,
plus a couple seconds of pickup latency).

Used in two places:
  1. From deploy.bat post-restart — gives the deploy a fast pass/fail
     signal that the per-call path actually works (catches the class
     of bug where the agent process is alive and "registered" but
     every call dies inside the entrypoint subprocess).
  2. From the iris-smoke-test.timer (every 3h) — catches operational
     failures that happen between deploys (LiveKit container died,
     Twilio billing issue, Deepgram quota, etc.).

Exit codes:
  0 — call completed and stayed up >= MIN_DURATION_S
  1 — call failed: status was not 'completed', or duration was too short
  2 — couldn't run the test (missing env var, Twilio API down, etc.)

SMS alerting on failure is currently STUBBED — the alert text is logged
with a WOULD-SEND-SMS prefix instead of actually being sent, because
SMS hasn't been approved by the user yet. When approved, uncomment the
real send in `_alert()`. The log line is intentionally easy to grep
(`journalctl -u iris-smoke-test.service | grep WOULD-SEND-SMS`).

Required env vars:
  TWILIO_ACCOUNT_SID     — Twilio account credentials (already in
  TWILIO_AUTH_TOKEN        iris-backend's .env if it sends Twilio
                           SIP/SMS today; reuses the same).
  IRIS_SMOKE_TEST_TO     — DID Iris answers on (e.g. +15419915071, dev DID).
  IRIS_SMOKE_TEST_FROM   — Twilio number to dial from (caller-ID on
                           the test call; will appear in the call
                           viewer dashboard with this caller-ID, so
                           pick something easy to filter on).
  IRIS_SMOKE_TEST_ALERT_TO — Cell number to SMS on failure. Currently
                             only logged, not sent.

Optional:
  IRIS_SMOKE_TEST_MIN_DURATION_S  — default 5
  IRIS_SMOKE_TEST_MAX_WAIT_S      — default 30
  IRIS_SMOKE_TEST_SMS_FROM        — Twilio SMS sender (used only once
                                    real SMS sending is enabled).
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone

from twilio.rest import Client as TwilioClient

log = logging.getLogger("iris_smoke_test")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

REQUIRED_ENV = (
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "IRIS_SMOKE_TEST_TO",
    "IRIS_SMOKE_TEST_FROM",
    "IRIS_SMOKE_TEST_ALERT_TO",
)
MIN_DURATION_S = int(os.environ.get("IRIS_SMOKE_TEST_MIN_DURATION_S", "5"))
MAX_WAIT_S = int(os.environ.get("IRIS_SMOKE_TEST_MAX_WAIT_S", "30"))
POLL_INTERVAL_S = 2


def _alert(message: str, recipient: str) -> None:
    """Surface a failure. Currently log-only — SMS pending user approval.

    When you're ready to enable real SMS:
      1. Confirm `IRIS_SMOKE_TEST_SMS_FROM` is set to a Twilio number
         that has SMS capability (a number bought as voice+SMS, or
         registered for A2P 10DLC if sending to US numbers).
      2. Confirm `IRIS_SMOKE_TEST_ALERT_TO` is your cell and you've
         consented to automated messages from this number.
      3. Uncomment the `client.messages.create(...)` block below.
      4. Re-deploy; the WOULD-SEND-SMS log lines will be replaced with
         actual sends.
    """
    log.warning(
        "WOULD-SEND-SMS (sending disabled — pending approval): "
        "to=%s message=%r",
        recipient, message,
    )
    # client = TwilioClient(
    #     os.environ["TWILIO_ACCOUNT_SID"],
    #     os.environ["TWILIO_AUTH_TOKEN"],
    # )
    # try:
    #     client.messages.create(
    #         to=recipient,
    #         from_=os.environ["IRIS_SMOKE_TEST_SMS_FROM"],
    #         body=message,
    #     )
    #     log.info("Alert SMS sent to %s", recipient)
    # except Exception:
    #     log.exception("Could not send alert SMS to %s", recipient)


def main() -> int:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        log.error("Missing required env vars: %s", missing)
        return 2

    to = os.environ["IRIS_SMOKE_TEST_TO"]
    frm = os.environ["IRIS_SMOKE_TEST_FROM"]
    alert_to = os.environ["IRIS_SMOKE_TEST_ALERT_TO"]

    log.info(
        "Placing smoke-test call: from=%s to=%s "
        "(min_duration=%ds, max_wait=%ds)",
        frm, to, MIN_DURATION_S, MAX_WAIT_S,
    )

    client = TwilioClient(
        os.environ["TWILIO_ACCOUNT_SID"],
        os.environ["TWILIO_AUTH_TOKEN"],
    )

    # TwiML: stay on the line for MIN_DURATION + a small buffer, then
    # hang up. Gives Iris time to answer + start her greeting; we hang
    # up before the conversation could go anywhere.
    pause_s = MIN_DURATION_S + 3
    twiml = f'<Response><Pause length="{pause_s}"/><Hangup/></Response>'

    started_at = datetime.now(timezone.utc)
    start_mono = time.monotonic()
    try:
        call = client.calls.create(to=to, from_=frm, twiml=twiml)
    except Exception:
        log.exception("Twilio calls.create() FAILED")
        msg = (
            f"Iris smoke test FAILED {started_at.isoformat(timespec='seconds')}: "
            f"could not place call via Twilio API. Service may be down. "
            f"Check Twilio account status."
        )
        _alert(msg, alert_to)
        return 1

    log.info("Call SID: %s — polling status...", call.sid)

    final_status: str | None = None
    duration_s = 0
    while time.monotonic() - start_mono < MAX_WAIT_S:
        time.sleep(POLL_INTERVAL_S)
        try:
            fetched = client.calls(call.sid).fetch()
        except Exception:
            log.exception("Twilio calls(...).fetch() failed; will retry")
            continue
        status = fetched.status
        log.info("  ...status=%s", status)
        if status in ("completed", "failed", "busy", "no-answer", "canceled"):
            final_status = status
            try:
                duration_s = int(fetched.duration or 0)
            except (TypeError, ValueError):
                duration_s = 0
            break

    if final_status is None:
        log.error(
            "Call did not complete within %ds wall-clock; counting as failure",
            MAX_WAIT_S,
        )
        msg = (
            f"Iris smoke test FAILED {started_at.isoformat(timespec='seconds')}: "
            f"call {call.sid} did not complete within {MAX_WAIT_S}s. "
            f"Either the call hung, LiveKit is unreachable, or the "
            f"dev DID isn't routing. Check journalctl -u iris-agent.service."
        )
        _alert(msg, alert_to)
        return 1

    log.info("Final status: %s, duration: %ds", final_status, duration_s)

    if final_status == "completed" and duration_s >= MIN_DURATION_S:
        log.info("PASS — Iris answered within threshold.")
        return 0

    msg = (
        f"Iris smoke test FAILED {started_at.isoformat(timespec='seconds')}: "
        f"call {call.sid} status={final_status} duration={duration_s}s "
        f"(needed status=completed AND duration>={MIN_DURATION_S}s). "
        f"Check journalctl -u iris-agent.service for the per-call error."
    )
    _alert(msg, alert_to)
    return 1


if __name__ == "__main__":
    sys.exit(main())
