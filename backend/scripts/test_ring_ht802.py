"""Place a Twilio API call directly to the SIP-registered HT802.

Verifies the inbound SIP path (Twilio → registered endpoint via NAT) works
before wiring Iris's transfer flow. When the desk phone is answered, a
short Polly TTS message plays and the call hangs up.

Usage (from project root):
    backend\\.venv\\Scripts\\python.exe backend\\scripts\\test_ring_ht802.py

Reads TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_HOTEL_NUMBER from
backend/.env via the existing app.config.settings. Uses httpx directly
against the Twilio REST API (matching the project's twilio_sms.py pattern)
rather than the Twilio Python SDK, which isn't a project dependency.
"""
import os
import sys
from pathlib import Path

backend_root = Path(__file__).parent.parent
os.chdir(backend_root)
sys.path.insert(0, str(backend_root))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402

SIP_TARGET = "sip:frontdesk@lighthouseinn-frontdesk.sip.twilio.com"

TWIML = (
    "<Response>"
    '<Say voice="Polly.Joanna">'
    "This is a test call from Twilio to the Lighthouse Inn front desk. "
    "The S I P connection is working. Goodbye."
    "</Say>"
    "<Hangup/>"
    "</Response>"
)


def main() -> None:
    if not settings.twilio_account_sid or not settings.twilio_auth_token:
        sys.exit("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN missing from backend/.env")
    if not settings.twilio_hotel_number:
        sys.exit("TWILIO_HOTEL_NUMBER missing from backend/.env")

    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/"
        f"{settings.twilio_account_sid}/Calls.json"
    )
    auth = (settings.twilio_account_sid, settings.twilio_auth_token)
    data = {
        "From": settings.twilio_hotel_number,
        "To": SIP_TARGET,
        "Twiml": TWIML,
    }

    response = httpx.post(url, auth=auth, data=data, timeout=10.0)

    if response.status_code not in (200, 201):
        sys.exit(f"Twilio API returned HTTP {response.status_code}: {response.text}")

    body = response.json()
    print(f"Call SID: {body.get('sid')}")
    print(f"Status:   {body.get('status')}")
    print(f"From:     {settings.twilio_hotel_number}")
    print(f"To:       {SIP_TARGET}")
    print()
    print("Desk phone should ring shortly. Pick up to hear the test message.")
    print("If nothing rings, check the HT802 syslog and the Twilio Console")
    print(f"call log for SID {body.get('sid')}.")


if __name__ == "__main__":
    main()
