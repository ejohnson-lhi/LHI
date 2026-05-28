"""LLM-backed "Ask Iris" fallback for the portal FAQ feature.

Called when the local matcher returns no good FAQ hits. Uses Anthropic's
Messages API directly (Haiku 4.5 with prompt caching) and offers the
model Anthropic's server-side web_search tool for current-events
questions that the static KB can't answer.

Design choices:

  - Single-turn: the portal Q&A box doesn't carry conversation history
    yet. Each "Ask Iris" call is independent. Multi-turn could be added
    later by passing the last N guest_qa rows; for v1, keeping it
    simple wins.

  - System prompt is the FAQ persona + the full knowledge_base.md. We
    set cache_control on the KB chunk so subsequent calls within the
    5-minute cache window are ~10% the original input cost.

  - No-confabulation rule baked into the system prompt: if the model
    doesn't know and the web search doesn't help, it says "I don't
    know" and points the guest at the front desk number.
"""
from __future__ import annotations

import logging

import anthropic

from app.config import settings
from app.services.faq_kb import get_faq_entries

log = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"
_MAX_TOKENS = 1024

_HOTEL_PHONE_FALLBACK = "541-997-3221"


def _build_kb_context() -> str:
    """Render the loaded FAQ entries as a single text block to drop into
    the system prompt. We rebuild from in-memory entries (rather than
    re-reading the markdown file) so a reload_faq_entries() call flows
    through to the next LLM call without a process restart."""
    chunks: list[str] = []
    for e in get_faq_entries():
        chunks.append(f"### {e.question}\n\n{e.answer}")
    return "\n\n".join(chunks)


def _system_prompt() -> tuple[str, str]:
    """Return (persona, kb_context) -- two halves of the system prompt.
    The kb_context piece is the long, expensive-to-tokenize chunk we mark
    `cache_control: ephemeral` so Anthropic's prompt cache covers it.
    Persona is the short, fast-changing piece (not cached)."""
    persona = (
        "You are Iris, the AI guest-portal helper for the Lighthouse Inn -- "
        "a small independent hotel in Florence, Oregon (phone "
        f"{_HOTEL_PHONE_FALLBACK}). A current guest is asking a question "
        "through the in-portal Q&A widget on their phone or laptop.\n\n"
        "Style: warm, concise, conversational. 1-3 short paragraphs.\n\n"
        "Sources, in priority order:\n"
        "  1. The Lighthouse Inn knowledge base below (authoritative for "
        "hotel policies, amenities, fees, room facts, local attractions).\n"
        "  2. The web_search tool, ONLY for current-events questions the KB "
        "can't answer (weather forecasts, restaurant hours today, festival "
        "schedules for this year, road closures). Don't web-search for "
        "things the KB already covers.\n\n"
        "No-confabulation rule: if neither source gives you a confident "
        "answer, say so plainly and direct the guest to call the front "
        f"desk at {_HOTEL_PHONE_FALLBACK}. Never invent policies, prices, "
        "or facts. \"I don't know\" is the right answer when you don't.\n\n"
        "Boundaries:\n"
        "  - You can answer questions, not perform actions. If a guest asks "
        "you to add a card or sign the agreement, point them at the "
        "relevant accordion in the portal.\n"
        "  - Don't quote or paste large blocks of the KB verbatim -- "
        "paraphrase tightly.\n"
        "  - Don't claim to be human."
    )
    kb_context = (
        "=== Lighthouse Inn knowledge base (use as primary source) ===\n\n"
        + _build_kb_context()
        + "\n\n=== End knowledge base ==="
    )
    return persona, kb_context


def _client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


async def ask_iris(question: str) -> dict:
    """Send a guest's question to the LLM. Returns a dict:
        {
          "answer": str,
          "input_tokens": int | None,
          "output_tokens": int | None,
          "web_search_used": bool,
          "error": str | None,    # set if the call failed
        }
    Never raises -- callers can render the error to the guest without
    a try/except wrapper. An error string means we couldn't get a
    useful answer and the guest should be told to call the front desk.
    """
    if not settings.anthropic_api_key:
        return {
            "answer": "",
            "input_tokens": None,
            "output_tokens": None,
            "web_search_used": False,
            "error": "Anthropic API key not configured.",
        }
    persona, kb_context = _system_prompt()
    # System is split into two blocks so cache_control applies only to
    # the (large, stable) KB. The persona stays uncached -- small enough
    # not to matter, and easy to tweak without a cache miss penalty.
    system_blocks = [
        {"type": "text", "text": persona},
        {
            "type": "text",
            "text": kb_context,
            "cache_control": {"type": "ephemeral"},
        },
    ]
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
    messages = [{"role": "user", "content": question}]
    try:
        resp = await _client().messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            system=system_blocks,
            messages=messages,
            tools=tools,
        )
    except anthropic.APIError as e:
        log.warning("Ask Iris: Anthropic error: %s", e)
        return {
            "answer": "",
            "input_tokens": None,
            "output_tokens": None,
            "web_search_used": False,
            "error": str(e),
        }

    # Anthropic returns content as a list of blocks (text + tool_use +
    # tool_result + ...). Concatenate the text blocks; track whether any
    # web_search was actually invoked. Tool calls don't reach the user
    # directly -- the model invokes them server-side, sees results, and
    # composes a final text answer.
    answer_parts: list[str] = []
    web_search_used = False
    for block in resp.content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            answer_parts.append(getattr(block, "text", ""))
        elif block_type == "server_tool_use":
            tool_name = getattr(block, "name", "")
            if tool_name == "web_search":
                web_search_used = True

    answer = "\n\n".join(p for p in answer_parts if p).strip()
    if not answer:
        answer = (
            "I'm not sure how to answer that. Please call the front desk at "
            f"{_HOTEL_PHONE_FALLBACK} -- they'll be glad to help."
        )

    usage = getattr(resp, "usage", None)
    in_tok = getattr(usage, "input_tokens", None) if usage else None
    out_tok = getattr(usage, "output_tokens", None) if usage else None
    return {
        "answer": answer,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "web_search_used": web_search_used,
        "error": None,
    }
