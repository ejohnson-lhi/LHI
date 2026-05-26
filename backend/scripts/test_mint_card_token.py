"""Mint a one-shot Add-Card URL for testing the token-authenticated
portal flow. Reads PORTAL_SHARED_SECRET from settings so you don't have
to fish it out of .env manually.

Usage (with uvicorn running locally on http://127.0.0.1:8100):
    .venv\\Scripts\\python.exe scripts\\test_mint_card_token.py [reservation_id] [room_number]

Defaults: reservation_id=1989264686165, room_number="4".

Outputs the portal URL — open it in your browser to test the full
token-authenticated card-capture flow end-to-end. The token is one-shot;
to test again, run this script for a fresh URL.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

DEFAULT_RESERVATION_ID = "1989264686165"
DEFAULT_ROOM = "4"
DEFAULT_FIRST_NAME = "Eric"
DEFAULT_PORT = 8100


async def main() -> int:
    from app.config import settings

    if not settings.portal_shared_secret:
        print("ERROR: PORTAL_SHARED_SECRET is not set in your .env.")
        print("       Generate with: python -c \"import secrets; print(secrets.token_urlsafe(32))\"")
        return 1

    reservation_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_RESERVATION_ID
    room_number = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_ROOM
    port = int(os.environ.get("UVICORN_PORT", DEFAULT_PORT))

    url = f"http://127.0.0.1:{port}/portal-card/mint"
    payload = {
        "reservation_id": reservation_id,
        "first_name": DEFAULT_FIRST_NAME,
        "room_number": room_number,
    }
    headers = {"X-Portal-Auth": settings.portal_shared_secret}

    print(f"POST {url}")
    print(f"  payload: {payload}")
    print()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.ConnectError:
        print(f"ERROR: couldn't connect to {url}.")
        print(f"       Is uvicorn running? Start it with:")
        print(f"       .venv\\Scripts\\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port {port} --reload")
        return 1

    if resp.status_code != 200:
        print(f"FAIL: HTTP {resp.status_code}")
        print(resp.text)
        return 1

    body = resp.json()
    print(f"OK. Mint response:")
    print(f"  token      = {body['token']}")
    print(f"  expires_at = {body['expires_at']}")
    print()
    print(f"Open this URL in a browser to test the flow:")
    print(f"  {body['portal_url']}")
    print()
    print(f"If portal_public_base_url in your .env points at the droplet, the URL")
    print(f"won't work locally. For local testing, manually rewrite to:")
    print(f"  http://127.0.0.1:{port}/portal-card/{body['token']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
