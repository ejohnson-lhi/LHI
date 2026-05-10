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

import json
import logging
import os
import uuid
from pathlib import Path

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
            "toolCallList": [
                {
                    "id": f"agent_{uuid.uuid4().hex[:12]}",
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
        # Speak the canonical greeting verbatim (not LLM-generated). Adds to
        # chat history by default so the LLM has context for the response.
        log.info("Agent on_enter: speaking first message")
        await self.session.say(FIRST_MESSAGE)

    # -------------------------------------------------------------------------
    # Tools — TEMPORARILY reduced to ONE for diagnostic. Anthropic was
    # rejecting our 7-tool schema as "too complex for compilation" even
    # with one-line docstrings and ctx:RunContext removed. If 1 tool works,
    # the issue is cumulative complexity and we restructure (collapse some
    # tools, use raw_schema, etc.). If 1 tool also fails, deeper digging.
    # The other 6 tools are kept as comments below — uncomment once we
    # solve the schema issue.
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

    # @function_tool
    # async def check_availability(self, check_in: str, check_out: str,
    #                              adults: int = 2, children: int = 0, rooms: int = 1) -> str:
    #     """Check available rooms and rates for a date range (YYYY-MM-DD)."""
    #     args = {"check_in": check_in, "check_out": check_out,
    #             "adults": adults, "children": children, "rooms": rooms}
    #     return json.dumps(await _call_backend_tool("check_availability", args, self._caller_phone))
    #
    # @function_tool
    # async def create_reservation(self, first_name: str, last_name: str, email: str,
    #                              check_in: str, check_out: str, room_type_id: str,
    #                              adults: int = 2, children: int = 0,
    #                              estimated_arrival_time: str = "", zip_code: str = "") -> str:
    #     """Create a Cloudbeds reservation using room_type_id from check_availability."""
    #     args = {"first_name": first_name, "last_name": last_name, "email": email,
    #             "check_in": check_in, "check_out": check_out, "room_type_id": room_type_id,
    #             "adults": adults, "children": children}
    #     if estimated_arrival_time: args["estimated_arrival_time"] = estimated_arrival_time
    #     if zip_code: args["zip_code"] = zip_code
    #     return json.dumps(await _call_backend_tool("create_reservation", args, self._caller_phone))
    #
    # @function_tool
    # async def add_reservation_note(self, reservation_id: str, note: str) -> str:
    #     """Append a note to an existing reservation."""
    #     args = {"reservation_id": reservation_id, "note": note}
    #     return json.dumps(await _call_backend_tool("add_reservation_note", args, self._caller_phone))
    #
    # @function_tool
    # async def modify_reservation(self, reservation_id: str,
    #                              new_check_out: str = "", estimated_arrival_time: str = "") -> str:
    #     """Update check-out date or arrival time on a direct-booking reservation."""
    #     args: dict = {"reservation_id": reservation_id}
    #     if new_check_out: args["new_check_out"] = new_check_out
    #     if estimated_arrival_time: args["estimated_arrival_time"] = estimated_arrival_time
    #     return json.dumps(await _call_backend_tool("modify_reservation", args, self._caller_phone))
    #
    # @function_tool
    # async def send_door_code(self, reservation_id: str, phone_number: str = "") -> str:
    #     """SMS the guest their room name and door code (defaults to caller-ID number)."""
    #     args: dict = {"reservation_id": reservation_id}
    #     if phone_number: args["phone_number"] = phone_number
    #     return json.dumps(await _call_backend_tool("send_door_code", args, self._caller_phone))
    #
    # @function_tool
    # async def cancel_reservation(self, reservation_id: str, reason: str = "") -> str:
    #     """Cancel a direct-booking reservation in Cloudbeds (irreversible)."""
    #     args: dict = {"reservation_id": reservation_id}
    #     if reason: args["reason"] = reason
    #     return json.dumps(await _call_backend_tool("cancel_reservation", args, self._caller_phone))


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
        # Sonnet 4.5 per quality requirement. Caching off for now — its
        # interaction with the tool block produced silent hangs; safe to
        # re-enable later once we land on stable tool schemas.
        llm=anthropic.LLM(model="claude-sonnet-4-5"),
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
    )

    await session.start(room=ctx.room, agent=IrisAgent(caller_phone=caller_phone))


if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint))
