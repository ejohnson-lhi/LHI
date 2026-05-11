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


def build_system_prompt(caller_phone: str | None = None) -> str:
    """Return the rendered system prompt for one call.

    The Knowledge Base is no longer inlined here; the agent's `inn_info`
    tool fetches relevant entries on demand. That removes ~7K tokens from
    every prompt the LLM processes (roughly 1s off cached TTFT) at the
    cost of an extra LLM round-trip on the subset of turns that need KB.
    """
    prompt = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
    return _render_placeholders(prompt, caller_phone=caller_phone)
