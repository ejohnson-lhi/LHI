"""Rule-based categorization of a call from its transcript.

Deterministic, fast, no LLM. Looks at which @function_tool methods
fired and at the last few messages to infer a category. Returns a list
of category tags (a call can have multiple — e.g., both 'reservation_started'
and 'transfer_to_front_desk' if the caller bailed out mid-flow).

Categories (with the trigger that asserts each):
  reservation_completed       create_reservation tool returned success
  reservation_started         check_availability fired but create_reservation
                              didn't (or didn't succeed)
  reservation_lookup          lookup_reservation fired
  reservation_modified        modify_reservation or add_reservation_note fired
  reservation_cancelled       cancel_reservation fired
  card_captured               capture_card_dtmf fired with success
  card_capture_failed         capture_card_dtmf fired without success
  door_code_sent              send_door_code fired
  transfer_to_front_desk      transfer_to fired with destination=front_desk
  transfer_to_eric            transfer_to fired with destination=eric
  inn_info                    inn_info fired (caller asked a hotel-fact question)
  admin                       admin_set_voice or admin_dtmf_mute_test fired
  silent_hangup               Caller said 1 or fewer turns; under 15 seconds
  short_call                  Under 30 seconds, more than 1 caller turn
  info_only                   No tools fired and call wasn't silent
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


# Each rule is (category_tag, predicate(events, items, duration) -> bool).
# Listed in roughly decreasing specificity so we can short-circuit on
# higher-signal ones if we ever care about that.


def _tool_calls(items: list[dict]) -> list[dict]:
    """All FunctionCall items in the chat history (= tool invocations)."""
    return [
        it for it in items
        if it.get("type") == "FunctionCall"
    ]


def _tool_outputs(items: list[dict]) -> list[dict]:
    """All FunctionCallOutput items in the chat history."""
    return [
        it for it in items
        if it.get("type") == "FunctionCallOutput"
    ]


def _tool_names(items: list[dict]) -> set[str]:
    """Set of tool names that fired this call."""
    return {
        str(it.get("name") or "")
        for it in _tool_calls(items)
        if it.get("name")
    }


def _tool_call_args(items: list[dict], tool_name: str) -> list[dict]:
    """Parsed `arguments` for all calls to a specific tool."""
    out: list[dict] = []
    import json
    for it in _tool_calls(items):
        if it.get("name") != tool_name:
            continue
        raw = it.get("arguments")
        if isinstance(raw, dict):
            out.append(raw)
        elif isinstance(raw, str):
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                out.append({})
        else:
            out.append({})
    return out


def _tool_output_for(items: list[dict], tool_name: str) -> list[dict]:
    """Parsed outputs of a given tool name.

    Tool outputs are linked to calls by call_id (or position). We don't
    bother correlating; we just collect every output whose preceding
    FunctionCall was the named tool. Good enough for status checks.
    """
    import json
    outputs: list[dict] = []
    last_tool_name: str | None = None
    for it in items:
        if it.get("type") == "FunctionCall":
            last_tool_name = str(it.get("name") or "")
        elif it.get("type") == "FunctionCallOutput":
            if last_tool_name == tool_name:
                raw = it.get("output")
                if isinstance(raw, dict):
                    outputs.append(raw)
                elif isinstance(raw, str):
                    try:
                        outputs.append(json.loads(raw))
                    except json.JSONDecodeError:
                        outputs.append({})
    return outputs


def _count_user_messages(items: list[dict]) -> int:
    """How many real user turns there were.

    We exclude the synthetic "blizzard frog" warmup (it's an assistant-
    generated marker, not a real caller utterance) and anything where
    the content is empty.
    """
    count = 0
    for it in items:
        if it.get("type") != "ChatMessage":
            continue
        if it.get("role") != "user":
            continue
        content = it.get("content")
        if isinstance(content, list):
            content = " ".join(str(c) for c in content)
        text = str(content or "").strip()
        if not text:
            continue
        if text.lower() == "blizzard frog":
            continue
        count += 1
    return count


def categorize(transcript: dict) -> list[str]:
    """Return a list of category tags for the call.

    Order is significant only for display (we emit specific tags first
    where possible). Empty list means we couldn't categorize anything,
    which is itself a signal worth surfacing in the UI.
    """
    items = transcript.get("items") or []
    duration = float(transcript.get("duration_seconds") or 0.0)
    tags: list[str] = []
    tool_names = _tool_names(items)

    # --- Reservation lifecycle ---
    create_outputs = _tool_output_for(items, "create_reservation")
    create_ok = any(
        (isinstance(o, dict) and (o.get("success") is True
                                  or "reservation_id" in o
                                  or o.get("status") == "success"))
        for o in create_outputs
    )
    if create_ok:
        tags.append("reservation_completed")
    elif "create_reservation" in tool_names:
        # create_reservation was attempted but didn't produce a success
        # marker — treat as "started, didn't finish"
        tags.append("reservation_started")
    elif "check_availability" in tool_names:
        # caller got at least to "show me a room", didn't book
        tags.append("reservation_started")

    if "lookup_reservation" in tool_names:
        tags.append("reservation_lookup")
    if "modify_reservation" in tool_names or "add_reservation_note" in tool_names:
        tags.append("reservation_modified")
    if "cancel_reservation" in tool_names:
        tags.append("reservation_cancelled")

    # --- Card capture ---
    capture_outputs = _tool_output_for(items, "capture_card_dtmf")
    if capture_outputs:
        success = any(
            isinstance(o, dict) and o.get("status") == "success"
            for o in capture_outputs
        )
        tags.append("card_captured" if success else "card_capture_failed")

    # --- Door code ---
    if "send_door_code" in tool_names:
        tags.append("door_code_sent")

    # --- Transfer destinations ---
    for args in _tool_call_args(items, "transfer_to"):
        dest = str(args.get("destination") or "").lower()
        if dest == "front_desk":
            tags.append("transfer_to_front_desk")
        elif dest == "eric":
            tags.append("transfer_to_eric")

    # --- Info questions ---
    if "inn_info" in tool_names:
        tags.append("inn_info")

    # --- Admin ---
    if "admin_set_voice" in tool_names or "admin_dtmf_mute_test" in tool_names:
        tags.append("admin")

    # --- Short / silent calls (only emit if nothing more specific did) ---
    user_msg_count = _count_user_messages(items)
    if not tags:
        if duration < 15 and user_msg_count <= 1:
            tags.append("silent_hangup")
        elif duration < 30 and user_msg_count >= 1:
            tags.append("short_call")
        else:
            tags.append("info_only")

    # De-dupe while preserving order. (List comprehension with seen-set.)
    seen: set[str] = set()
    deduped: list[str] = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return deduped
