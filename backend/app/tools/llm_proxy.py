"""OpenAI-compatible chat-completions proxy that forwards to Anthropic.

Vapi calls our `/llm/chat/completions` endpoint as if it were OpenAI. We
translate to Anthropic's Messages API, add `cache_control` to the system
prompt so prompt caching applies, and stream the response back as
OpenAI-compatible SSE chunks.
"""
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator

import anthropic

from app.config import settings

log = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 4096
# Hardcoded for now — Vapi's model.model field is opaque to us anyway since
# this proxy is the LLM from Vapi's perspective. Switch by editing this line.
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"


def _client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


def _openai_to_anthropic(openai_request: dict) -> dict:
    """Translate an OpenAI chat-completions request to an Anthropic Messages call.

    Pulls system messages out into Anthropic's separate `system` field with
    cache_control set so the (large) Iris prompt is cached. Translates tool
    calls + tool results to Anthropic's content-block conventions.
    """
    raw_messages = openai_request.get("messages") or []

    system_chunks: list[str] = []
    chat_messages: list[dict] = []
    for msg in raw_messages:
        if msg.get("role") == "system":
            content = msg.get("content")
            if isinstance(content, str):
                system_chunks.append(content)
            elif isinstance(content, list):
                # In case some caller sends content as blocks
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        system_chunks.append(block.get("text", ""))
        else:
            chat_messages.append(msg)

    anthropic_messages: list[dict] = []
    for msg in chat_messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "user":
            if isinstance(content, str):
                anthropic_messages.append({"role": "user", "content": content})
            elif isinstance(content, list):
                anthropic_messages.append({"role": "user", "content": content})
            else:
                anthropic_messages.append({"role": "user", "content": ""})

        elif role == "assistant":
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                blocks: list[dict] = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    args = fn.get("arguments")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args) if args else {}
                        except json.JSONDecodeError:
                            args = {}
                    elif args is None:
                        args = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id"),
                        "name": fn.get("name"),
                        "input": args,
                    })
                anthropic_messages.append({"role": "assistant", "content": blocks})
            else:
                anthropic_messages.append({"role": "assistant", "content": content or ""})

        elif role == "tool":
            # Tool result → user-role message with a tool_result content block.
            tool_call_id = msg.get("tool_call_id")
            anthropic_messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_call_id,
                    "content": content if isinstance(content, str) else json.dumps(content),
                }],
            })

    # Tools: OpenAI uses {type, function:{name, description, parameters}};
    # Anthropic uses {name, description, input_schema}.
    anthropic_tools: list[dict] = []
    for tool in openai_request.get("tools") or []:
        if tool.get("type") != "function":
            continue
        fn = tool.get("function") or {}
        anthropic_tools.append({
            "name": fn.get("name"),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })

    request: dict = {
        "model": ANTHROPIC_MODEL,
        "messages": anthropic_messages,
        "max_tokens": int(openai_request.get("max_tokens") or DEFAULT_MAX_TOKENS),
    }
    if system_chunks:
        request["system"] = [{
            "type": "text",
            "text": "\n\n".join(system_chunks),
            "cache_control": {"type": "ephemeral"},
        }]
    if anthropic_tools:
        request["tools"] = anthropic_tools
    if (temp := openai_request.get("temperature")) is not None:
        request["temperature"] = temp
    return request


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


async def stream_chat_completion(openai_request: dict) -> AsyncIterator[str]:
    """Stream an OpenAI-compatible chat completion by forwarding to Anthropic."""
    if not settings.anthropic_api_key:
        yield _sse({"error": {"message": "ANTHROPIC_API_KEY not configured"}})
        yield "data: [DONE]\n\n"
        return

    request = _openai_to_anthropic(openai_request)
    completion_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    response_model = openai_request.get("model") or ANTHROPIC_MODEL

    def chunk(delta: dict, finish_reason: str | None = None) -> str:
        return _sse({
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": response_model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        })

    # Initial chunk announcing the assistant role.
    yield chunk({"role": "assistant", "content": ""})

    tool_call_index = -1
    finish_reason: str | None = None

    try:
        async with _client().messages.stream(**request) as stream:
            async for event in stream:
                etype = getattr(event, "type", None)

                if etype == "content_block_start":
                    block = getattr(event, "content_block", None)
                    if block is not None and getattr(block, "type", None) == "tool_use":
                        tool_call_index += 1
                        yield chunk({
                            "tool_calls": [{
                                "index": tool_call_index,
                                "id": block.id,
                                "type": "function",
                                "function": {"name": block.name, "arguments": ""},
                            }]
                        })

                elif etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    dtype = getattr(delta, "type", None) if delta is not None else None
                    if dtype == "text_delta":
                        yield chunk({"content": delta.text})
                    elif dtype == "input_json_delta":
                        yield chunk({
                            "tool_calls": [{
                                "index": tool_call_index,
                                "function": {"arguments": delta.partial_json},
                            }]
                        })

                elif etype == "message_delta":
                    delta = getattr(event, "delta", None)
                    stop_reason = getattr(delta, "stop_reason", None) if delta is not None else None
                    finish_reason = {
                        "end_turn": "stop",
                        "tool_use": "tool_calls",
                        "max_tokens": "length",
                        "stop_sequence": "stop",
                    }.get(stop_reason or "", "stop")

    except anthropic.APIError as e:
        log.exception("Anthropic API error in stream_chat_completion")
        yield _sse({"error": {"message": str(e)}})
        finish_reason = finish_reason or "stop"

    yield chunk({}, finish_reason=finish_reason or "stop")
    yield "data: [DONE]\n\n"
