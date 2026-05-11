"""Load Iris's system prompt + KB and render date / caller-phone placeholders.

This is the LiveKit-side equivalent of `backend/scripts/sync_to_vapi.py`'s
`build_system_prompt()` + `_render_placeholders()`. Kept as a separate copy
(rather than imported from backend) so the agent doesn't need the backend's
venv or its dependencies.

When the prompt format changes, update both copies.
"""
from datetime import datetime, timedelta
from pathlib import Path

# Repo layout:  <repo>/AI_Prompts/...   <repo>/agent/iris_prompt.py
PROMPTS_DIR = Path(__file__).parent.parent / "AI_Prompts"
SYSTEM_PROMPT_FILE = PROMPTS_DIR / "Lighthouse_AI_system_prompt-2026may02.txt"
# Knowledge base is now loaded by agent/inn_info.py and exposed as a tool
# rather than inlined here. See build_system_prompt() docstring.


def _render_placeholders(prompt: str, caller_phone: str | None = None) -> str:
    now = datetime.now()
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
        # Caller phone: substituted at agent construction time, per call.
        # Vapi version sets this to "{{customer.number}}" so Vapi can do per-call
        # substitution on its side. We do per-call substitution here directly.
        "{{caller_phone_number}}": caller_phone or "",
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


_ADMIN_BLOCK = """

[Admin Mode]

The caller is authorized as the system administrator. They may issue admin commands beyond the normal call flow.

**Voice switch**: When the admin says "Switch to the [name] voice" or similar, call `admin_set_voice` with that name. The tool accepts voice nicknames (sarah, santa, aoede, eric), persona names (Iris, Henry, Aoede, Eric), or internal model keys (af_sarah, am_santa, etc.) — pass whatever the admin spoke. The change applies to the NEXT call, not the current one.

**After the tool returns success**, the response contains a `persona_name` field. Confirm to the admin using EXACTLY that returned name — NOT your own current persona name, NOT the name in the admin's request:

> "Voice set to [persona_name from response]. It will apply to your next call."

Critical: do not substitute your own name. If the response says `"persona_name": "Iris"`, say "Voice set to Iris" even if you are currently Henry. If the response says `"persona_name": "Henry"`, say "Voice set to Henry".

Voice/persona reference (for translating admin requests):
- voice `sarah` ↔ persona Iris (default female)
- voice `santa` ↔ persona Henry (male)
- voice `aoede` ↔ persona Aoede (female, lighter)
- voice `eric` ↔ persona Eric (male, lighter)

For other admin-style requests outside the listed commands, politely decline and continue as a normal Lighthouse Inn call.
"""


def _substitute_persona(text: str, persona: str) -> str:
    """Replace "Iris" with `persona` everywhere except in the proper-noun
    name of the phonetic alphabet ("Iris phonetic alphabet" / "Iris
    alphabet"). No-op if persona is "Iris"."""
    if persona == "Iris":
        return text
    # Temporary stand-ins for the proper-noun mentions so the bare
    # "Iris" replace doesn't touch them. Use \x00 + tag so they can't
    # collide with anything in the actual prompt content.
    PROTECTED_1 = "\x00IRIS_PHONETIC_ALPHABET\x00"
    PROTECTED_2 = "\x00IRIS_ALPHABET\x00"
    text = text.replace("Iris phonetic alphabet", PROTECTED_1)
    text = text.replace("Iris alphabet", PROTECTED_2)
    text = text.replace("Iris", persona)
    text = text.replace(PROTECTED_1, "Iris phonetic alphabet")
    text = text.replace(PROTECTED_2, "Iris alphabet")
    return text


def build_system_prompt(
    caller_phone: str | None = None,
    is_admin: bool = False,
    persona: str = "Iris",
) -> str:
    """Return the rendered system prompt for one call.

    `persona` is the name the agent calls itself (Iris by default, Henry
    when using the am_santa voice, etc.). All self-references to "Iris"
    in the prompt template get replaced with `persona`, except the proper-
    noun mentions of the "Iris phonetic alphabet" / "Iris alphabet" which
    are kept as-is regardless of voice.

    If `is_admin` is True, an [Admin Mode] section is appended.
    """
    prompt = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
    if is_admin:
        prompt = prompt + _ADMIN_BLOCK
    prompt = _substitute_persona(prompt, persona)
    return _render_placeholders(prompt, caller_phone=caller_phone)
