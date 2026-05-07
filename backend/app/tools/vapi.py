"""Vapi API client wrapper.

Auth: `Authorization: <api_key>` (no Bearer prefix) against api.vapi.ai.
"""
import logging

import httpx

from app.config import settings

log = logging.getLogger(__name__)

VAPI_BASE_URL = "https://api.vapi.ai"
# Assistant updates are server-side configuration changes — slower than tool calls.
DEFAULT_TIMEOUT_SECONDS = 30.0


def _auth_headers() -> dict[str, str]:
    return {"Authorization": settings.vapi_api_key}


async def get_assistant(assistant_id: str | None = None) -> dict | None:
    """Fetch the current Vapi assistant config, or None on failure."""
    aid = assistant_id or settings.vapi_assistant_id
    if not aid or not settings.vapi_api_key:
        log.warning("Vapi assistant ID or API key not configured")
        return None
    url = f"{VAPI_BASE_URL}/assistant/{aid}"
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers=_auth_headers())
    except httpx.HTTPError as e:
        log.warning("Vapi get_assistant HTTP error: %s", e)
        return None
    if response.status_code != 200:
        log.warning("Vapi get_assistant HTTP %s: %s", response.status_code, response.text[:300])
        return None
    return response.json()


async def update_assistant(patch_data: dict, assistant_id: str | None = None) -> dict | None:
    """PATCH the Vapi assistant with new config; returns updated assistant or None."""
    aid = assistant_id or settings.vapi_assistant_id
    if not aid or not settings.vapi_api_key:
        log.warning("Vapi assistant ID or API key not configured")
        return None
    url = f"{VAPI_BASE_URL}/assistant/{aid}"
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.patch(url, json=patch_data, headers=_auth_headers())
    except httpx.HTTPError as e:
        log.warning("Vapi update_assistant HTTP error: %s", e)
        return None
    if response.status_code != 200:
        log.warning("Vapi update_assistant HTTP %s: %s", response.status_code, response.text[:500])
        return None
    return response.json()
