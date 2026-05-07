"""Custom LLM endpoint that Vapi calls instead of Anthropic directly.

Vapi sends OpenAI-format chat completion requests; we forward to Anthropic
with cache_control on the system prompt so prompt caching applies, then
stream the response back as OpenAI-compatible SSE chunks.

Vapi appends `/chat/completions` to whatever URL is configured on the
assistant's `model.url`, so we mount this router at `/llm`.
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.tools.llm_proxy import stream_chat_completion

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    log.info(
        "llm proxy: model=%s msgs=%d tools=%d stream=%s",
        body.get("model"),
        len(body.get("messages") or []),
        len(body.get("tools") or []),
        body.get("stream"),
    )
    return StreamingResponse(
        stream_chat_completion(body),
        media_type="text/event-stream",
    )
