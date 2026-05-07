"""Admin endpoints for backend monitoring and configuration.

These are HTTP-accessible (not the admin-by-phone commands — those are handled
through Vapi tool calls in vapi_tools.py with SIP header authentication).
"""
from fastapi import APIRouter

from app.tools import cloudbeds, vapi

router = APIRouter()


@router.get("/vapi_assistant")
async def vapi_assistant():
    """Fetch the current Vapi assistant config (for inspection / debugging)."""
    cfg = await vapi.get_assistant()
    if cfg is None:
        return {"success": False, "message": "Could not fetch Vapi assistant."}
    return {"success": True, "assistant": cfg}


@router.get("/vapi_phone_numbers")
async def vapi_phone_numbers():
    """List phone-number resources currently registered with Vapi.

    Useful to see whether a Twilio number is already linked to the Iris
    assistant or whether we need to create one.
    """
    import httpx

    from app.config import settings as _settings
    if not _settings.vapi_api_key:
        return {"success": False, "message": "VAPI_API_KEY not configured."}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://api.vapi.ai/phone-number",
                headers={"Authorization": _settings.vapi_api_key},
            )
    except httpx.HTTPError as e:
        return {"success": False, "message": str(e)}
    if resp.status_code != 200:
        return {"success": False, "status": resp.status_code, "body": resp.text[:500]}
    return {"success": True, "phone_numbers": resp.json()}


@router.get("/cloudbeds_sources")
async def cloudbeds_sources():
    """List Cloudbeds reservation sources for this property.

    Used to find the sourceID for things like the VoiceAI source we created
    in the dashboard.
    """
    sources = await cloudbeds.list_sources()
    if sources is None:
        return {"success": False, "message": "Could not list sources."}
    return {"success": True, "sources": sources}


@router.get("/cloudbeds_users")
async def cloudbeds_users():
    """List Cloudbeds users for this property.

    Useful for finding userIDs to pass as note attribution etc.
    """
    users = await cloudbeds.list_users()
    if users is None:
        return {"success": False, "message": "Could not list users."}
    return {"success": True, "users": users}


@router.get("/status")
async def admin_status():
    """Return current call-routing state and basic system status.

    TODO: read from DB.
    """
    return {
        "call_routing": {
            "mode": "ai_handle",
            "destination": None,
            "expires_at": None,
            "set_by": None,
        },
        "block_list_count": 0,
        "_stub": True,
    }
