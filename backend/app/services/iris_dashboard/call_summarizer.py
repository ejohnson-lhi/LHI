"""Claude-generated narrative summary of a call, cached as a sidecar JSON.

ONE Anthropic call per processed call. Output is a short prose summary
plus a structured outcome assessment. Stored at
`recordings/summary_{call_id}.json` next to the transcript so the
dashboard can read it instantly on the next view.

INPUT to the LLM: the chat history items (only role/content; we skip
tool args because they have PII like card-last-4 we don't need in the
summary cache), plus a brief stats block (duration, item count, tool
names that fired).

OUTPUT shape (JSON, stored verbatim in sidecar):
{
  "summary": "Caller asked for a reservation Friday-Sunday, Iris ...",
  "outcome": "reservation_completed" | "reservation_incomplete" | ...,
  "issues_observed": ["Iris repeated the closing line",
                       "Caller had to repeat their phone number twice"],
  "generated_at": "2026-05-29T19:08:41Z",
  "generator_version": "v1",
  "anthropic_model": "claude-sonnet-4-5",
  "input_token_count": 1234,
  "output_token_count": 89
}

Auth: reuses the same ANTHROPIC_API_KEY env var the agent uses. No
separate credentials.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

GENERATOR_VERSION = "v2"
# Haiku 4.5 instead of Sonnet 4.5: ~4x cheaper input ($0.80/M vs $3/M),
# ~4x cheaper output ($4/M vs $15/M). Verified 2026-05-29 that
# claude-haiku-4-5 resolves on the droplet's Anthropic key. Summary
# quality has been good enough in spot checks; flip back to
# claude-sonnet-4-5 here if it ever feels off.
ANTHROPIC_MODEL = "claude-haiku-4-5"

# Cost per call with Haiku 4.5: ~3000 input tokens * $0.80/M + ~200
# output tokens * $4/M = ~$0.0032 per call. At ~100 calls/month that's
# ~$0.32/month. Trivial for the value of auto-attached call context
# on each reservation.
SUMMARY_PROMPT = """You are reviewing a phone call between an AI receptionist named Iris (Lighthouse Inn, a small coastal hotel in Florence, Oregon) and a caller.

Read the transcript below and produce a JSON object with these fields:

- "summary": ONE paragraph (2-4 sentences) describing what the caller wanted and what happened. Plain English, no jargon. Avoid restating tool names; describe the outcome in terms a hotel owner would care about.

- "outcome": one of: "reservation_completed", "reservation_incomplete", "info_only", "transfer_to_front_desk", "transfer_to_eric", "silent_hangup", "card_capture_completed", "card_capture_failed", "other". Pick the single tag that best describes the call's primary outcome.

- "issues_observed": array of short (under 12 word) strings describing any quality problems you noticed -- Iris repeating herself, mishearing the caller, taking too long, going down the wrong path, etc. EMPTY ARRAY if the call went smoothly.

Return ONLY the JSON object. No prose before or after."""


def _format_transcript_for_llm(transcript: dict) -> str:
    """Render the chat history as a compact text block.

    We include role + content for each ChatMessage and a one-line note
    for each FunctionCall. FunctionCallOutput bodies are summarized to
    success/failure to keep the prompt small and avoid PII (card last-4,
    door codes, etc.).
    """
    lines: list[str] = []
    items = transcript.get("items") or []
    for it in items:
        kind = it.get("type")
        if kind == "ChatMessage":
            role = it.get("role") or ""
            content = it.get("content")
            if isinstance(content, list):
                content = " ".join(str(c) for c in content)
            text = str(content or "").strip()
            if not text:
                continue
            if text.lower() == "blizzard frog":
                continue  # internal warmup, not real
            if role == "assistant" and text.lower() == "hello":
                continue  # warmup response, also not real
            lines.append(f"{role.upper()}: {text}")
        elif kind == "FunctionCall":
            name = it.get("name") or "<unknown>"
            lines.append(f"[Iris called tool: {name}]")
        elif kind == "FunctionCallOutput":
            raw = it.get("output")
            ok = False
            if isinstance(raw, dict):
                ok = bool(
                    raw.get("success") is True
                    or raw.get("status") in ("success", "connected", "ok")
                    or "reservation_id" in raw
                )
            elif isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                    ok = bool(
                        parsed.get("success") is True
                        or parsed.get("status") in ("success", "connected", "ok")
                        or "reservation_id" in parsed
                    )
                except (json.JSONDecodeError, AttributeError):
                    pass
            lines.append(f"[Tool result: {'success' if ok else 'failure or partial'}]")

    if not lines:
        return "(no recorded turns)"
    return "\n".join(lines)


async def summarize(transcript: dict, *, api_key: str | None = None) -> dict:
    """Generate a structured summary by calling Claude.

    Returns the parsed JSON dict (matches the schema in this module's
    docstring) or raises on irrecoverable errors. The route layer is
    responsible for catching and surfacing exceptions to the UI.
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    transcript_text = _format_transcript_for_llm(transcript)
    duration = transcript.get("duration_seconds") or 0.0
    started_at = transcript.get("started_at") or "?"
    user_input = (
        f"Call started at {started_at}, lasted {duration:.0f}s.\n\n"
        f"Transcript:\n\n{transcript_text}"
    )

    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 600,
        "system": SUMMARY_PROMPT,
        "messages": [
            {"role": "user", "content": user_input},
        ],
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            json=body,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Anthropic API HTTP {resp.status_code}: {resp.text[:500]}"
        )

    data = resp.json()
    # content is a list of blocks; we expect a single text block with JSON.
    blocks = data.get("content") or []
    text = ""
    for b in blocks:
        if b.get("type") == "text":
            text += b.get("text") or ""
    text = text.strip()
    # Be lenient: the model sometimes wraps the JSON in a code fence even
    # though we asked it not to. Strip common fences before parsing.
    if text.startswith("```"):
        # remove leading ``` and language hint up through the next newline
        text = text.split("\n", 1)[-1] if "\n" in text else text
        if text.endswith("```"):
            text = text[: -3]
        text = text.strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Anthropic returned non-JSON: {e}; raw={text[:500]}")

    usage = data.get("usage") or {}
    parsed["generated_at"] = datetime.now(timezone.utc).isoformat()
    parsed["generator_version"] = GENERATOR_VERSION
    parsed["anthropic_model"] = ANTHROPIC_MODEL
    parsed["input_token_count"] = int(usage.get("input_tokens") or 0)
    parsed["output_token_count"] = int(usage.get("output_tokens") or 0)
    return parsed


def write_sidecar(sidecar_path: Path, summary: dict) -> None:
    """Write the summary to disk atomically (write to .tmp, rename)."""
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = sidecar_path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    os.replace(tmp, sidecar_path)
