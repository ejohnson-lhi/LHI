"""Push local Iris config (system prompt + KB + tool specs) to the Vapi assistant.

Usage (from backend dir):
    python scripts/sync_to_vapi.py <public_url>

Where <public_url> is the public-facing root of the backend, e.g. the
trycloudflare.com hostname when running locally. The script appends
/tools/<name> to that for each tool's webhook URL.

Reads VAPI_API_KEY and VAPI_ASSISTANT_ID from .env, plus the prompt + KB
from the AI_Prompts folder one level above the backend project.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# The hotel is in Florence, Oregon — Pacific time. Date placeholders
# ({{current_datetime_long}}, {{tomorrow}}, etc.) must be computed in
# the hotel's local timezone, not UTC. The droplet runs UTC; without
# this fix the prompt says "tomorrow" anytime between 5pm Pacific
# (DST: 4pm) and midnight Pacific. Mirrors agent/iris_prompt.py.
_HOTEL_TZ = ZoneInfo("America/Los_Angeles")
from pathlib import Path

backend_root = Path(__file__).parent.parent
os.chdir(backend_root)
sys.path.insert(0, str(backend_root))

from app.config import settings  # noqa: E402
from app.tools import vapi  # noqa: E402

# Prompt files live in a sibling folder to backend/, not inside it.
PROMPTS_DIR = backend_root.parent / "AI_Prompts"
SYSTEM_PROMPT_FILE = PROMPTS_DIR / "Lighthouse_AI_system_prompt-2026may02.txt"
KB_FILE = PROMPTS_DIR / "knowledge_base.md"

ASSISTANT_NAME = "Iris"
FIRST_MESSAGE = "Lighthouse Inn, this is Iris, the AI assistant. How may I help you?"

# Vapi defaults backgroundSound to 'office' on phone calls (typing sounds for
# "presence"). Hotel callers hear that as unprofessional — keep it off.
BACKGROUND_SOUND = "off"

# To pick a different ElevenLabs voice: browse https://elevenlabs.io/voice-library,
# copy the voice ID, paste below. Run sync to apply. None = leave whatever Vapi
# currently has on the assistant (so you can also pick from Vapi's UI).
VOICE_PROVIDER: str | None = "11labs"
VOICE_ID: str | None = "vr5WKaGvRWsoaX5LCVax"  # Cherie R — fewer pauses around OTA names than Chantel
VOICE_MODEL: str | None = "eleven_turbo_v2_5"


def build_tool_specs(public_url: str) -> list[dict]:
    base = public_url.rstrip("/")
    return [
        {
            "type": "function",
            "function": {
                "name": "lookup_reservation",
                "description": (
                    "Look up an existing reservation. Tries the most specific "
                    "identifier provided: source_reservation_id (OTA confirmation "
                    "number from Expedia/Booking.com/etc.) > phone_number > "
                    "caller-ID. Returns a summary including dates, room, door "
                    "code, source, is_direct_booking flag, and stay_phase "
                    "(future / arriving_today / in_house / "
                    "in_house_departing_tomorrow / departing_today / past)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone_number": {
                            "type": "string",
                            "description": "E.164 phone (e.g., +15417295563). Optional — caller-ID is used by default.",
                        },
                        "source_reservation_id": {
                            "type": "string",
                            "description": "OTA confirmation number (Expedia, Booking.com, etc.). Use whenever the caller mentions one.",
                        },
                        "last_name": {
                            "type": "string",
                            "description": "Last name fallback (not yet implemented).",
                        },
                    },
                },
            },
            "server": {"url": f"{base}/tools/lookup_reservation"},
        },
        {
            "type": "function",
            "function": {
                "name": "check_availability",
                "description": (
                    "Check available room types and rates for a date range. "
                    "Returns a list of room types with their rate plans sorted "
                    "cheapest-first; quote rate_plans[0] for the best deal."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "check_in": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                        "check_out": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                        "adults": {"type": "integer", "description": "Defaults to 2."},
                        "children": {"type": "integer", "description": "Defaults to 0."},
                        "rooms": {"type": "integer", "description": "Defaults to 1."},
                    },
                    "required": ["check_in", "check_out"],
                },
            },
            "server": {"url": f"{base}/tools/check_availability"},
        },
        {
            "type": "function",
            "function": {
                "name": "create_reservation",
                "description": (
                    "Create a new reservation in Cloudbeds. Use the room_type_id "
                    "from a prior check_availability call. Returns reservation_id, "
                    "status, and grand_total on success."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "first_name": {"type": "string"},
                        "last_name": {"type": "string"},
                        "email": {"type": "string", "description": "Use 'none@test.com' if guest declines to provide one."},
                        "check_in": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                        "check_out": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                        "room_type_id": {"type": "string", "description": "From check_availability response."},
                        "adults": {"type": "integer", "description": "Defaults to 2."},
                        "children": {"type": "integer", "description": "Defaults to 0."},
                        "estimated_arrival_time": {"type": "string", "description": "24h format e.g. '20:00'. Optional."},
                        "zip_code": {"type": "string", "description": "Optional."},
                    },
                    "required": ["first_name", "last_name", "email", "check_in", "check_out", "room_type_id"],
                },
            },
            "server": {"url": f"{base}/tools/create_reservation"},
        },
        {
            "type": "function",
            "function": {
                "name": "add_reservation_note",
                "description": (
                    "Append a note to an existing reservation (e.g., special "
                    "requests, late arrival info, etc.). Notes are attributed to "
                    "the Iris Agent user in Cloudbeds."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reservation_id": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["reservation_id", "note"],
                },
            },
            "server": {"url": f"{base}/tools/add_reservation_note"},
        },
        {
            "type": "function",
            "function": {
                "name": "modify_reservation",
                "description": (
                    "Modify an existing direct-booking reservation. v1 supports "
                    "extending/shortening the stay (new_check_out) and updating "
                    "estimated arrival time. CRITICAL: only call for direct "
                    "bookings (lookup_reservation must show is_direct_booking=true). "
                    "For OTA reservations redirect to the OTA. For check-IN date "
                    "changes (rare — guest can't arrive on original day), this "
                    "tool can't help; offer to transfer to front desk. For room "
                    "type / bed count changes, also transfer."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reservation_id": {
                            "type": "string",
                            "description": "Cloudbeds reservation ID from a prior lookup_reservation.",
                        },
                        "new_check_out": {
                            "type": "string",
                            "description": "New check-out date in ISO YYYY-MM-DD. Use for extending or shortening the stay. Verify availability with check_availability first if extending.",
                        },
                        "estimated_arrival_time": {
                            "type": "string",
                            "description": "Updated estimated arrival time in 24-hour HH:MM format (e.g., '20:00').",
                        },
                    },
                    "required": ["reservation_id"],
                },
            },
            "server": {"url": f"{base}/tools/modify_reservation"},
        },
        {
            "type": "function",
            "function": {
                "name": "send_door_code",
                "description": (
                    "Send the guest their room name and door code via SMS. "
                    "ONLY call after the lockout self-service two-factor "
                    "auth (caller-ID match + verbal room number match) has "
                    "succeeded — see [Lockout self-service] in the prompt. "
                    "Do not call without that authentication. By default the "
                    "SMS goes to the caller's phone number; only override "
                    "phone_number if the caller asks to send to a different "
                    "verified number on the reservation."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reservation_id": {
                            "type": "string",
                            "description": "Cloudbeds reservation ID from a prior lookup_reservation.",
                        },
                        "phone_number": {
                            "type": "string",
                            "description": "Optional override; defaults to the caller's number (caller-ID).",
                        },
                    },
                    "required": ["reservation_id"],
                },
            },
            "server": {"url": f"{base}/tools/send_door_code"},
        },
        {
            "type": "function",
            "function": {
                "name": "cancel_reservation",
                "description": (
                    "Cancel a reservation in Cloudbeds. CRITICAL: only call for "
                    "DIRECT bookings — first call lookup_reservation and verify "
                    "is_direct_booking is true. For OTA reservations (Expedia, "
                    "Booking.com, Hotels.com), DO NOT call this — instead tell "
                    "the caller to contact the OTA directly. Cancellations are "
                    "irreversible. Always confirm with the caller before calling."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reservation_id": {
                            "type": "string",
                            "description": "Cloudbeds reservation ID from a prior lookup_reservation call.",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Optional cancellation reason; saved as a note on the reservation for audit.",
                        },
                    },
                    "required": ["reservation_id"],
                },
            },
            "server": {"url": f"{base}/tools/cancel_reservation"},
        },
    ]


def _render_placeholders(prompt: str) -> str:
    """Substitute the prompt's `{{...}}` placeholders before pushing to Vapi.

    Dates are computed at sync time — re-run sync daily to keep them fresh
    (or hourly if `{{current_datetime_long}}` precision matters within a day).
    The caller-phone placeholder is rewritten to Vapi's built-in
    `{{customer.number}}` so Vapi substitutes it per call automatically.
    Unfilled feature-flag placeholders become empty strings.
    """
    now = datetime.now(_HOTEL_TZ)
    today = now.date()

    def fmt_long(d) -> str:
        # "Sunday, May 4, 2026" — cross-platform (no %-d / %#d).
        return d.strftime("%A, %B ") + str(d.day) + d.strftime(", %Y")

    def fmt_time(dt) -> str:
        h = dt.hour % 12 or 12
        return f"{h}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"

    weekdays = {
        "sunday": 6, "monday": 0, "tuesday": 1, "wednesday": 2,
        "thursday": 3, "friday": 4, "saturday": 5,
    }

    def next_weekday(target_idx: int):
        # If today IS that weekday, "next X" means a week from today.
        days_ahead = (target_idx - today.weekday() + 7) % 7
        if days_ahead == 0:
            days_ahead = 7
        return today + timedelta(days=days_ahead)

    replacements: dict[str, str] = {
        "{{current_datetime_long}}": f"{fmt_long(today)} at {fmt_time(now)}",
        "{{current_day}}": fmt_long(today),
        "{{tomorrow}}": fmt_long(today + timedelta(days=1)),
        "{{day_plus_2}}": fmt_long(today + timedelta(days=2)),
        "{{day_plus_3}}": fmt_long(today + timedelta(days=3)),
        "{{day_plus_4}}": fmt_long(today + timedelta(days=4)),
        "{{day_plus_5}}": fmt_long(today + timedelta(days=5)),
        "{{day_plus_6}}": fmt_long(today + timedelta(days=6)),
        # Caller phone: rewrite to Vapi's runtime variable. Vapi substitutes
        # {{customer.number}} from the call's caller-ID at call time.
        "{{caller_phone_number}}": "{{customer.number}}",
        # Unused feature flags — leave empty so the LLM doesn't see literal `{{...}}`.
        "{{after_hours}}": "",
        "{{customer_retargeting_consent_rules}}": "",
        "{{call_closure_guideline_customer_retargeting}}": "",
    }
    for day_name, idx in weekdays.items():
        replacements[f"{{{{next_{day_name}}}}}"] = fmt_long(next_weekday(idx))

    rendered = prompt
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def build_system_prompt() -> str:
    prompt = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
    kb = KB_FILE.read_text(encoding="utf-8")
    combined = f"{prompt}\n\n# Knowledge Base\n\n{kb}"
    return _render_placeholders(combined)


async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/sync_to_vapi.py <public_url>")
        print("  e.g.: python scripts/sync_to_vapi.py https://noble-editors-sending-roberts.trycloudflare.com")
        sys.exit(2)

    public_url = sys.argv[1]
    if not settings.vapi_api_key or not settings.vapi_assistant_id:
        print("ERROR: VAPI_API_KEY and VAPI_ASSISTANT_ID must be set in .env")
        sys.exit(2)

    # Pull current assistant so we preserve model settings (provider, model,
    # temperature, etc.) we aren't explicitly overwriting.
    current = await vapi.get_assistant()
    if current is None:
        print("ERROR: could not fetch current assistant — check API key + assistant ID")
        sys.exit(1)
    current_model = current.get("model") or {}

    prompt = build_system_prompt()
    tools = build_tool_specs(public_url)

    # Custom LLM mode: Vapi calls our /llm/chat/completions endpoint as if
    # it were OpenAI. Our proxy translates to Anthropic with cache_control
    # on the system prompt so prompt caching applies (which Vapi's stock
    # Anthropic provider doesn't expose). The "model" field is informational
    # at this layer — the proxy decides which Anthropic model to call.
    new_model = {
        "provider": "custom-llm",
        "url": f"{public_url.rstrip('/')}/llm",
        "model": "anthropic/claude-haiku-4-5-20251001",
        "maxTokens": current_model.get("maxTokens", 500),
        "messages": [{"role": "system", "content": prompt}],
        "tools": tools,
    }
    patch: dict = {
        "name": ASSISTANT_NAME,
        "firstMessage": FIRST_MESSAGE,
        "model": new_model,
        "backgroundSound": BACKGROUND_SOUND,
    }
    if VOICE_PROVIDER and VOICE_ID:
        voice_cfg: dict = {"provider": VOICE_PROVIDER, "voiceId": VOICE_ID}
        if VOICE_MODEL:
            voice_cfg["model"] = VOICE_MODEL
        patch["voice"] = voice_cfg

    print(f"Patching Vapi assistant id={settings.vapi_assistant_id}")
    print(f"  Public URL: {public_url}")
    print(f"  System prompt: {len(prompt):,} chars (= prompt {SYSTEM_PROMPT_FILE.name} + kb {KB_FILE.name})")
    print(f"  Tools: {[t['function']['name'] for t in tools]}")
    print(f"  First message: {FIRST_MESSAGE!r}")
    print(f"  Model preserved: provider={current_model.get('provider')} model={current_model.get('model')}")

    result = await vapi.update_assistant(patch)
    if result is None:
        print("FAILED — check FastAPI log for the Vapi PATCH error")
        sys.exit(1)
    print()
    print(f"Done. Updated assistant '{result.get('name')}' at {result.get('updatedAt')}")


if __name__ == "__main__":
    asyncio.run(main())
