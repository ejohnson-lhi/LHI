"""Full Iris LiveKit agent — production hotel reservation receptionist.

Pipeline:
    Twilio inbound (+15419915071) -> LiveKit SIP -> room
        -> IrisAgent (this file) -> Deepgram STT (Nova-3, telephony-tuned)
                                 -> Claude Sonnet 4.5 (with prompt caching)
                                 -> tools call backend FastAPI via httpx
                                 -> Kokoro TTS (af_sarah) -> back to caller

Run via systemd:
    systemctl start iris-agent

Or in foreground for dev:
    .venv/bin/python iris_agent.py dev

Required env (in agent/.env):
    LIVEKIT_URL          ws://127.0.0.1:7880
    LIVEKIT_API_KEY      from /opt/livekit/livekit.yaml
    LIVEKIT_API_SECRET   from /opt/livekit/livekit.yaml
    ANTHROPIC_API_KEY    same as backend/.env
    DEEPGRAM_API_KEY     from console.deepgram.com
    IRIS_BACKEND_URL     defaults to http://127.0.0.1:8000
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

import anthropic as anthropic_sdk  # raw SDK, used to construct a custom client
import httpx
from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import Agent, AgentSession, JobContext, function_tool
from livekit.agents.voice.turn import InterruptionOptions, TurnHandlingOptions
from livekit.plugins import anthropic, deepgram, silero

from iris_prompt import build_system_prompt
from kokoro_tts import KokoroTTS

load_dotenv()
log = logging.getLogger("iris")
logging.basicConfig(level=logging.INFO)

HERE = Path(__file__).parent
KOKORO_MODEL = HERE / "models" / "kokoro-v1.0.onnx"
KOKORO_VOICES = HERE / "models" / "voices-v1.0.bin"

BACKEND_URL = os.environ.get("IRIS_BACKEND_URL", "http://127.0.0.1:8000")
BACKEND_TIMEOUT_S = 15.0

# Where to write per-call transcripts. Gitignored. Each call gets a single
# JSON file with the full chat history + timestamps so we can investigate
# latency, prompt issues, and tool-call failures after the fact.
TRANSCRIPTS_DIR = Path(os.environ.get(
    "IRIS_TRANSCRIPTS_DIR", "/opt/iris-backend/recordings"
))

# First message text. Same wording the previous Vapi setup used. Spoken
# verbatim (not LLM-generated) so the greeting is consistent across calls.
FIRST_MESSAGE = "Lighthouse Inn, this is Iris, the AI assistant. How may I help you?"


# =============================================================================
# Backend-tool HTTP helper
# =============================================================================


async def _call_backend_tool(
    name: str,
    args: dict,
    caller_phone: str | None,
) -> dict:
    """POST to backend's /tools/<name> using the Vapi envelope format.

    Backend's existing tool routes (backend/app/routes/vapi_tools.py) expect
    Vapi-shape requests. Constructing the envelope here keeps the backend
    untouched — same code path serves Vapi (legacy) and the LiveKit agent.
    """
    payload = {
        "message": {
            "type": "tool-calls",  # required by backend's VapiToolCallMessage model
            "toolCallList": [
                {
                    "id": f"agent_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {"name": name, "arguments": args},
                }
            ],
            "call": {
                "customer": ({"number": caller_phone} if caller_phone else {}),
            },
        }
    }
    url = f"{BACKEND_URL}/tools/{name}"
    log.info("backend tool call: %s args=%s", name, args)
    try:
        async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT_S) as client:
            resp = await client.post(url, json=payload)
    except httpx.TimeoutException:
        log.warning("backend tool %s timed out", name)
        return {"error": "Backend timed out — try again."}
    except httpx.HTTPError as e:
        log.warning("backend tool %s http error: %s", name, e)
        return {"error": f"Backend connection error: {e}"}

    if resp.status_code != 200:
        log.warning("backend tool %s HTTP %s: %s", name, resp.status_code, resp.text[:300])
        return {"error": f"Backend error HTTP {resp.status_code}"}

    body = resp.json()
    try:
        # Vapi response: {"results": [{"toolCallId", "name", "result": "<json string>"}]}
        return json.loads(body["results"][0]["result"])
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        log.warning("could not parse backend response for %s: %s body=%s", name, e, body)
        return {"error": "Could not parse backend response."}


# =============================================================================
# IrisAgent — tools as methods, auto-registered by find_function_tools()
# =============================================================================


class IrisAgent(Agent):
    """Iris, the Lighthouse Inn AI receptionist.

    Constructed once per call. Tools are decorated methods, auto-discovered
    by livekit-agents' `find_function_tools()`. The caller's phone number
    is captured at construction (from SIP participant attributes) and used
    both in the system prompt (`{{caller_phone_number}}` substitution) and
    as the default phone for tool calls that need caller-ID.
    """

    def __init__(self, caller_phone: str | None) -> None:
        super().__init__(instructions=build_system_prompt(caller_phone=caller_phone))
        self._caller_phone = caller_phone

    async def on_enter(self) -> None:
        # Wait briefly for the audio path between LiveKit and the SIP gateway
        # to settle. Without this, on_enter fires the moment Kokoro finishes
        # loading — before livekit-sip has fully bridged audio with the
        # caller — and the greeting plays into a void. Caller experience:
        # call connects with no ringback, then silence.
        await asyncio.sleep(1.5)
        log.info("Agent on_enter: speaking first message")
        await self.session.say(FIRST_MESSAGE)

    # -------------------------------------------------------------------------
    # Tools — JSON return values, all proxied to backend's existing handlers.
    # Schema complexity tolerated via `_strict_tool_schema=False` on the LLM
    # (see comment at session construction). With strict mode off, Anthropic's
    # 24-optional-param-across-all-tools limit doesn't apply.
    # -------------------------------------------------------------------------

    @function_tool
    async def lookup_reservation(
        self,
        phone_number: str = "",
        source_reservation_id: str = "",
        last_name: str = "",
    ) -> str:
        """Look up an existing reservation by OTA ID, phone, or last name."""
        args = {
            k: v for k, v in {
                "phone_number": phone_number,
                "source_reservation_id": source_reservation_id,
                "last_name": last_name,
            }.items() if v
        }
        return json.dumps(await _call_backend_tool("lookup_reservation", args, self._caller_phone))

    @function_tool
    async def check_availability(
        self,
        check_in: str,
        check_out: str,
        adults: int = 2,
        children: int = 0,
        rooms: int = 1,
    ) -> str:
        """Check available rooms and rates for a date range (YYYY-MM-DD)."""
        args = {
            "check_in": check_in,
            "check_out": check_out,
            "adults": adults,
            "children": children,
            "rooms": rooms,
        }
        return json.dumps(await _call_backend_tool("check_availability", args, self._caller_phone))

    @function_tool
    async def create_reservation(
        self,
        first_name: str,
        last_name: str,
        email: str,
        check_in: str,
        check_out: str,
        room_type_id: str,
        adults: int = 2,
        children: int = 0,
        estimated_arrival_time: str = "",
        zip_code: str = "",
    ) -> str:
        """Create a Cloudbeds reservation using room_type_id from check_availability."""
        args = {
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "check_in": check_in,
            "check_out": check_out,
            "room_type_id": room_type_id,
            "adults": adults,
            "children": children,
        }
        if estimated_arrival_time:
            args["estimated_arrival_time"] = estimated_arrival_time
        if zip_code:
            args["zip_code"] = zip_code
        return json.dumps(await _call_backend_tool("create_reservation", args, self._caller_phone))

    @function_tool
    async def add_reservation_note(
        self,
        reservation_id: str,
        note: str,
    ) -> str:
        """Append a note to an existing reservation."""
        args = {"reservation_id": reservation_id, "note": note}
        return json.dumps(await _call_backend_tool("add_reservation_note", args, self._caller_phone))

    @function_tool
    async def modify_reservation(
        self,
        reservation_id: str,
        new_check_out: str = "",
        estimated_arrival_time: str = "",
    ) -> str:
        """Update check-out date or arrival time on a direct-booking reservation."""
        args: dict = {"reservation_id": reservation_id}
        if new_check_out:
            args["new_check_out"] = new_check_out
        if estimated_arrival_time:
            args["estimated_arrival_time"] = estimated_arrival_time
        return json.dumps(await _call_backend_tool("modify_reservation", args, self._caller_phone))

    @function_tool
    async def send_door_code(
        self,
        reservation_id: str,
        phone_number: str = "",
    ) -> str:
        """SMS the guest their room name and door code (defaults to caller-ID number)."""
        args: dict = {"reservation_id": reservation_id}
        if phone_number:
            args["phone_number"] = phone_number
        return json.dumps(await _call_backend_tool("send_door_code", args, self._caller_phone))

    @function_tool
    async def cancel_reservation(
        self,
        reservation_id: str,
        reason: str = "",
    ) -> str:
        """Cancel a direct-booking reservation in Cloudbeds (irreversible)."""
        args: dict = {"reservation_id": reservation_id}
        if reason:
            args["reason"] = reason
        return json.dumps(await _call_backend_tool("cancel_reservation", args, self._caller_phone))


# =============================================================================
# Entrypoint — wired up at worker registration time
# =============================================================================


async def entrypoint(ctx: JobContext) -> None:
    log.info("Job received for room %s", ctx.room.name)
    await ctx.connect()

    # Wait for the SIP participant to join the room so we can pull the
    # caller's phone number off their attributes. SIP participants come in
    # with `kind == PARTICIPANT_KIND_SIP` and a `sip.phoneNumber` attribute.
    participant = await ctx.wait_for_participant()
    caller_phone: str | None = None
    if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
        caller_phone = participant.attributes.get("sip.phoneNumber")
    log.info("Caller participant=%s phone=%s kind=%s",
             participant.identity, caller_phone, participant.kind)

    session = AgentSession(
        vad=silero.VAD.load(),
        stt=deepgram.STT(model="nova-3"),
        # Sonnet 4.5 with three workarounds for known livekit-plugins-anthropic
        # bugs (see research notes / GitHub issues):
        #
        # (1) `client=` with relaxed timeouts.
        #     The plugin hardcodes `httpx.AsyncClient(timeout=5.0)` — a 5-sec
        #     flat budget that includes per-chunk SSE read. Sonnet thinking
        #     phases routinely exceed this. Filed as livekit/agents#5508,
        #     PR #5529 unmerged. Workaround: build our own client with a
        #     60-sec read timeout via the public `client=` parameter
        #     (livekit/agents#4129 / PR #4143 made this work in 1.5.x).
        #
        # (2) `_strict_tool_schema=False`.
        #     Plugin defaults to strict-mode tool schemas (livekit/agents#5162
        #     since 1.5.2). Strict mode triggers Anthropic's grammar compiler,
        #     which has a documented hard limit of 24 optional parameters
        #     across all tool schemas per request. We have ~28 across 7 tools,
        #     so every API call returns "Schema is too complex for compilation".
        #     Disabling strict gives us back the looser type validation —
        #     fine for Sonnet, which adheres to types reliably.
        #
        # (3) `caching="ephemeral"`.
        #     Marks the system prompt + tool block with cache_control. With
        #     our 30K-token prompt, this is roughly a 3x cost reduction after
        #     the first cache write per call (which itself is ~1.25x base).
        llm=anthropic.LLM(
            model="claude-sonnet-4-5",
            client=anthropic_sdk.AsyncClient(
                api_key=os.environ["ANTHROPIC_API_KEY"],
                http_client=httpx.AsyncClient(
                    timeout=httpx.Timeout(5.0, read=60.0, write=10.0, pool=10.0),
                    follow_redirects=True,
                    limits=httpx.Limits(
                        max_connections=1000,
                        max_keepalive_connections=100,
                        keepalive_expiry=120,
                    ),
                ),
            ),
            caching="ephemeral",
            _strict_tool_schema=False,
        ),
        tts=KokoroTTS(
            model_path=str(KOKORO_MODEL),
            voices_path=str(KOKORO_VOICES),
            voice="af_sarah",
        ),
        # Skip LiveKit Cloud's adaptive interruption (cloud-only feature
        # that 401s on self-hosted). VAD-based interruption works fine.
        turn_handling=TurnHandlingOptions(
            interruption=InterruptionOptions(mode="vad"),
        ),
        # Disable preemptive generation to avoid wasted LLM calls when STT
        # finalization shifts. With our 30K-token prompt, each restarted
        # generation is expensive. Trade-off: ~200ms extra latency at end of
        # user utterance, in exchange for not double-paying for restarts.
        preemptive_generation=False,
    )

    # Log LLM metrics after each turn so we can verify prompt caching is
    # actually hitting (cache_read_input_tokens should be high relative to
    # input_tokens). Helps diagnose whether the 12-sec post-tool latency is
    # cache-miss or just Sonnet being slow.
    @session.on("metrics_collected")
    def _on_metrics(ev) -> None:
        m = getattr(ev, "metrics", None)
        if m is None:
            return
        # LLMMetrics, STTMetrics, TTSMetrics — log a compact one-liner
        # with whatever attrs are present.
        kind = type(m).__name__
        attrs = {}
        for k in ("ttft", "duration", "prompt_tokens", "completion_tokens",
                  "cache_creation_input_tokens", "cache_read_input_tokens",
                  "input_tokens", "output_tokens", "audio_duration",
                  "characters_count", "tokens_per_second"):
            v = getattr(m, k, None)
            if v is not None:
                attrs[k] = round(v, 3) if isinstance(v, float) else v
        if attrs:
            log.info("metrics %s: %s", kind, attrs)

    started_at = datetime.now()

    async def write_transcript() -> None:
        """On shutdown, dump session.history to JSON for after-the-fact review.

        Captures full chat context (system prompt + every user/assistant turn
        + tool calls + tool results) plus timestamps. Useful for debugging
        latency, prompt regressions, and tool-call failures.
        """
        try:
            TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
            ts = started_at.strftime("%Y%m%d_%H%M%S")
            phone_safe = (caller_phone or "unknown").lstrip("+") or "unknown"
            out = TRANSCRIPTS_DIR / f"transcript_{ts}_{phone_safe}.json"

            # session.history is a ChatContext; .items is the list of turns.
            items = []
            for item in session.history.items:
                # ChatContext items vary by class (ChatMessage, FunctionCall,
                # FunctionCallOutput); serialize whatever attrs we find.
                d: dict = {"type": type(item).__name__}
                for attr in ("id", "role", "content", "name", "arguments",
                             "call_id", "output", "interrupted", "created_at"):
                    if hasattr(item, attr):
                        v = getattr(item, attr)
                        # Content is often a list of text blocks; flatten to str.
                        if attr == "content" and isinstance(v, list):
                            v = " ".join(
                                getattr(b, "text", str(b)) for b in v
                            )
                        d[attr] = v
                items.append(d)

            transcript = {
                "room": ctx.room.name,
                "caller_phone": caller_phone,
                "started_at": started_at.isoformat(),
                "ended_at": datetime.now().isoformat(),
                "duration_seconds": (datetime.now() - started_at).total_seconds(),
                "item_count": len(items),
                "items": items,
            }
            out.write_text(json.dumps(transcript, indent=2, default=str))
            log.info("Transcript written: %s (%d items)", out, len(items))
        except Exception:
            # Never let transcript writing crash session shutdown.
            log.exception("Failed to write transcript")

    ctx.add_shutdown_callback(write_transcript)

    await session.start(room=ctx.room, agent=IrisAgent(caller_phone=caller_phone))


if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint))
