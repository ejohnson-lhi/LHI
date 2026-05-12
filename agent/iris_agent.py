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
from datetime import datetime, timedelta
from pathlib import Path

import anthropic as anthropic_sdk  # raw SDK, used to construct a custom client
import numpy as np
import httpx
from dotenv import load_dotenv
from livekit import agents, api, rtc
from livekit.agents import Agent, AgentSession, JobContext, JobProcess, function_tool
from livekit.agents.voice.turn import InterruptionOptions, TurnHandlingOptions
from livekit.plugins import anthropic, deepgram, silero

import inn_info
from audio_cache import TTSAudioCache
from iris_prompt import build_system_prompt
from kokoro_tts import KokoroTTS

load_dotenv()
log = logging.getLogger("iris")
logging.basicConfig(level=logging.INFO)

HERE = Path(__file__).parent
KOKORO_MODEL = HERE / "models" / "kokoro-v1.0.onnx"
KOKORO_VOICES = HERE / "models" / "voices-v1.0.bin"

# Which Kokoro voice Iris speaks with. The admin can switch this live by
# calling Iris and saying "switch to the Henry voice" — see admin_set_voice
# below. Resolution order: ~/.cache/iris/voice.txt (last admin choice) →
# IRIS_VOICE env var → "af_sarah" default.
#
# Friendly-name → internal-voice map. The LLM sees these nicknames in the
# admin prompt block and translates the spoken name to the internal key
# when calling admin_set_voice. Nicknames follow the Kokoro model pattern
# (af_sarah → sarah, am_santa → santa) so they're easy to remember.
VOICE_NICKNAMES: dict[str, str] = {
    "sarah": "af_sarah",
    "santa": "am_santa",
    "aoede": "af_aoede",
    "eric":  "am_eric",
}

# Per-voice persona name — what Iris calls herself when using that voice.
# Decoupled from the voice MODEL nickname so "Santa" voice (Kokoro model
# am_santa) can be introduced to callers as "Henry" rather than "Santa".
# Used in the greeting and substituted throughout the system prompt so
# the LLM consistently refers to itself by the persona name.
PERSONA_NAMES: dict[str, str] = {
    "af_sarah": "Iris",
    "am_santa": "Henry",
    "af_aoede": "Aoede",
    "am_eric":  "Eric",
}


def _persona_for(voice: str) -> str:
    return PERSONA_NAMES.get(voice, "Iris")

VOICE_STATE_FILE = Path.home() / ".cache" / "iris" / "voice.txt"


def _resolve_voice() -> str:
    """Pick voice from state file → env var → default. Tolerates a missing
    or corrupt state file."""
    if VOICE_STATE_FILE.exists():
        try:
            v = VOICE_STATE_FILE.read_text().strip()
            if v:
                return v
        except OSError:
            pass
    return os.environ.get("IRIS_VOICE", "af_sarah")


# Admin's caller-ID phone number. When a call arrives from this number,
# the agent flags is_admin=True and the system prompt gets the [Admin Mode]
# block (instructions for handling voice-switch and other admin commands).
# Empty/unset = no admin recognized.
ADMIN_PHONE = os.environ.get("IRIS_ADMIN_PHONE", "")

# LiveKit outbound SIP trunks. transfer_to currently uses OUTBOUND_TRUNK_ID
# (Twilio PSTN Termination) for all destinations via warm-bridge. The
# FRONTDESK_TRUNK_ID (SIP Domain) is still configured on LiveKit but unused
# by the agent — direct INVITEs through it returned 403 Forbidden from
# Twilio regardless of From value (Twilio's SIP Domain doesn't accept
# external authenticated INVITEs targeting registered SIP endpoints). Kept
# in the env so re-enabling is a one-line change if/when we find a working
# HT802 routing path (Twilio DID dedicated to HT802, or TwiML on the SIP
# Domain Voice URL).
OUTBOUND_TRUNK_ID = os.environ.get("IRIS_OUTBOUND_TRUNK_ID", "")
FRONTDESK_TRUNK_ID = os.environ.get("IRIS_FRONTDESK_TRUNK_ID", "")

# Warm-transfer destinations. Both currently route to Eric's cell via the
# PSTN trunk (front_desk is temporarily aliased to Eric until a working
# HT802 routing path exists).
#
# Why warm-bridge (create_sip_participant) instead of SIP REFER:
#   The LiveKit-side recording captures the room's audio. With warm-bridge,
#   Iris stays in the room while the human takes over, so the recording
#   captures the full conversation — including the human-handled portion.
#   That's invaluable right now for iterating on the LLM prompt. With REFER,
#   Iris drops at the moment Twilio accepts the REFER, ending the recording.
#
# Trade-off: the destination sees +15419915071 (Iris's Twilio DID) as
# caller-ID, not the original caller's number. We accept that for now;
# revisit once the prompt is stable enough that recording every transfer
# is no longer essential.
TRANSFER_TARGETS: dict[str, tuple[str, str]] = {
    "front_desk": ("+15412286786", "the front desk"),
    "eric":       ("+15412286786", "Eric"),
}

# Max time to wait for the destination to pick up before treating as
# no-answer and returning to the LLM so it can try the fallback.
TRANSFER_RING_TIMEOUT_S = 30

# Disk-backed TTS audio cache. LiveKit spawns a fresh worker subprocess for
# each call (with num_idle_processes=1), so cache survives only across
# calls via disk. Lives under ~/.cache/ since systemd's ProtectSystem=strict
# allows /home/iris/.cache (already in the unit's ReadWritePaths).
TTS_CACHE_PATH = Path.home() / ".cache" / "iris" / "tts_cache.pkl"

# Subfolder of the recordings dir where we ALSO dump each cached entry as
# a WAV file at shutdown — handy for listening to what's in the cache and
# spotting TTS mispronunciations. The recordings dir is already in
# ReadWritePaths and is synced to Windows by sync_recordings.bat.
TTS_CACHE_WAV_DIR = Path(os.environ.get(
    "IRIS_TTS_CACHE_WAV_DIR", "/opt/iris-backend/recordings/tts_cache"
))

BACKEND_URL = os.environ.get("IRIS_BACKEND_URL", "http://127.0.0.1:8000")
BACKEND_TIMEOUT_S = 15.0

# Where to write per-call transcripts. Gitignored. Each call gets a single
# JSON file with the full chat history + timestamps so we can investigate
# latency, prompt issues, and tool-call failures after the fact.
TRANSCRIPTS_DIR = Path(os.environ.get(
    "IRIS_TRANSCRIPTS_DIR", "/opt/iris-backend/recordings"
))

# First message template. The persona name is substituted per voice
# (Iris for af_sarah, Henry for am_santa, etc.). Spoken verbatim (not
# LLM-generated) so the greeting is consistent across calls.
FIRST_MESSAGE_TEMPLATE = "Lighthouse Inn, this is {persona}, the AI assistant. How may I help you?"


def _first_message_for(persona: str) -> str:
    return FIRST_MESSAGE_TEMPLATE.format(persona=persona)


def _greeting_chunks_for(persona: str) -> list[str]:
    """Sentence-split version of the greeting — what LiveKit's TTS layer
    actually calls .synthesize() with. Each chunk gets pre-rendered into
    the cache at prewarm so the greeting plays as cache hits (instant)."""
    return [
        f"Lighthouse Inn, this is {persona}, the AI assistant.",
        "How may I help you?",
    ]

# Synthetic ringback tone (440 + 480 Hz dual-tone, ~1.5s) played as the
# agent's very first audio. LiveKit-SIP answers calls with 200 OK
# immediately so Twilio can't play real PSTN ringback — this brief
# fake ringback fills the silent-connect gap and gives callers the
# familiar "one ring, then pickup" UX. Cached under a sentinel key
# that no LLM output will ever match, then played via session.say()
# which finds the cache hit and bypasses Kokoro entirely.
RINGBACK_CACHE_KEY = "__ringback_tone__"
RINGBACK_DURATION_S = 1.5


def _generate_ringback_pcm(duration_s: float = RINGBACK_DURATION_S) -> bytes:
    """24 kHz mono int16 PCM bytes of US ringback tone, with short
    fade-in/out to avoid pops. Suitable for direct insertion into the
    KokoroTTS cache (same format as Kokoro's output)."""
    sr = 24000
    n = int(sr * duration_s)
    t = np.linspace(0, duration_s, n, endpoint=False, dtype=np.float32)
    # North American ringback: 440 Hz + 480 Hz at equal volume.
    tone = 0.25 * (np.sin(2 * np.pi * 440 * t) + np.sin(2 * np.pi * 480 * t))
    fade = int(sr * 0.05)  # 50 ms fade
    tone[:fade] *= np.linspace(0, 1, fade)
    tone[-fade:] *= np.linspace(1, 0, fade)
    pcm16 = (np.clip(tone, -1.0, 1.0) * 32767.0).astype(np.int16)
    return pcm16.tobytes()


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

    def __init__(
        self,
        caller_phone: str | None,
        persona: str = "Iris",
        room: rtc.Room | None = None,
    ) -> None:
        self._is_admin = bool(ADMIN_PHONE) and caller_phone == ADMIN_PHONE
        self._persona = persona
        # AgentSession in livekit-agents 1.5.8 doesn't expose `room` as a
        # public attribute, so we capture ctx.room here. transfer_to needs
        # the room name to dial an outbound SIP participant into it.
        self._room = room
        super().__init__(instructions=build_system_prompt(
            caller_phone=caller_phone,
            is_admin=self._is_admin,
            persona=persona,
        ))
        self._caller_phone = caller_phone
        if self._is_admin:
            log.info("Caller %s recognized as admin", caller_phone)

    async def on_enter(self) -> None:
        # Brief wait for the SIP audio bridge to settle. Without it the
        # first audio plays into a void on some carriers.
        await asyncio.sleep(0.3)
        # Play a synthetic ringback tone first so the caller hears a
        # familiar "one ring" before the agent speaks — LiveKit-SIP
        # answers with 200 OK immediately so Twilio can't generate real
        # PSTN ringback. The tone is pre-cached in prewarm(), so this is
        # an instant cache hit, not a synthesis call.
        log.info("Agent on_enter: playing ringback tone")
        await self.session.say(RINGBACK_CACHE_KEY, allow_interruptions=False)
        # Greeting uses the persona name for the current voice. Cache key
        # is voice-aware so this is also a cache hit (pre-rendered in
        # prewarm or entrypoint when voice changed).
        voice = self.session.tts._opts.voice
        persona = _persona_for(voice)
        first_message = _first_message_for(persona)
        log.info("Agent on_enter: speaking first message (voice=%s, persona=%s)", voice, persona)
        # allow_interruptions=False so the greeting plays even if the
        # caller speaks first.
        await self.session.say(first_message, allow_interruptions=False)

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

    @function_tool
    async def inn_info(self, question: str) -> str:
        """Look up Lighthouse Inn details (room features, amenities, pet/smoking/parking/breakfast policy, local area, transit, hours, etc.). Use for any guest question not covered directly by your other tools or the inline system prompt."""
        return inn_info.lookup(question)

    @function_tool
    async def transfer_to(self, destination: str) -> str:
        """Warm-transfer the caller to a human. `destination` is 'front_desk' or 'eric'. Iris stays on the call (silent) while the destination is connected, so the LiveKit recording captures the full conversation. The destination sees the hotel number as caller-ID, not the original caller's number. Returns JSON: status='connected' (destination joined the call) or status='no_answer' (timeout or rejected). On no_answer, follow the [Transfer Scope Rules] fallback: try the other destination if appropriate."""

        if not OUTBOUND_TRUNK_ID:
            log.error("Transfer requested but IRIS_OUTBOUND_TRUNK_ID not set")
            return json.dumps({
                "error": "Outbound calling is not configured on the agent. Cannot transfer.",
            })
        target = TRANSFER_TARGETS.get(destination)
        if target is None:
            return json.dumps({
                "error": f"Unknown destination: {destination!r}",
                "valid_destinations": list(TRANSFER_TARGETS),
            })

        sip_to, label = target
        if self._room is None:
            log.error("Transfer requested but agent has no room reference")
            return json.dumps({"error": "Internal: no room available."})
        room_name = self._room.name
        log.info("Warm-bridge transfer: %s -> %s (room=%s)", destination, sip_to, room_name)

        try:
            lk = api.LiveKitAPI()
            try:
                await lk.sip.create_sip_participant(
                    api.CreateSIPParticipantRequest(
                        sip_trunk_id=OUTBOUND_TRUNK_ID,
                        sip_call_to=sip_to,
                        room_name=room_name,
                        participant_identity=f"transfer-{destination}",
                        participant_name=label,
                        play_dialtone=True,
                        wait_until_answered=True,
                        ringing_timeout=timedelta(seconds=TRANSFER_RING_TIMEOUT_S),
                    )
                )
            finally:
                await lk.aclose()
        except Exception:
            log.exception("Transfer to %s failed", destination)
            return json.dumps({
                "status": "no_answer",
                "destination": destination,
                "display": label,
            })

        log.info("Transfer to %s connected", destination)
        return json.dumps({
            "status": "connected",
            "destination": destination,
            "display": label,
        })

    @function_tool
    async def admin_set_voice(self, voice: str) -> str:
        """[Admin only] Switch Iris's voice for the NEXT call. `voice` accepts a voice nickname ('sarah', 'santa', 'aoede', 'eric'), a persona name ('Iris', 'Henry', 'Aoede', 'Eric'), or the internal Kokoro key ('af_sarah', 'am_santa', etc.). Returns a JSON object with persona_name — use that name verbatim when confirming the change to the admin."""
        if not self._is_admin:
            return json.dumps({"error": "Not authorized."})

        v = voice.strip()
        v_lower = v.lower()

        # Resolution order: voice nickname → persona name → assume internal.
        resolved = VOICE_NICKNAMES.get(v_lower)
        if resolved is None:
            # Match against persona names (case-insensitive).
            for vm, pn in PERSONA_NAMES.items():
                if pn.lower() == v_lower:
                    resolved = vm
                    break
        if resolved is None:
            resolved = v  # last resort: assume it's an internal voice key

        # Validate against what Kokoro actually loaded.
        kokoro = self.session.tts._kokoro
        available = set(kokoro.get_voices())
        if resolved not in available:
            return json.dumps({
                "error": f"Unknown voice: {voice}",
                "valid_voice_nicknames": list(VOICE_NICKNAMES),
                "valid_persona_names": list(PERSONA_NAMES.values()),
            })
        try:
            VOICE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            VOICE_STATE_FILE.write_text(resolved)
        except OSError as e:
            return json.dumps({"error": f"Could not write voice state: {e}"})

        new_persona = PERSONA_NAMES.get(resolved, "Iris")
        log.info("Admin set voice to %s (persona=%s); applies next call", resolved, new_persona)
        return json.dumps({
            "status": "ok",
            "voice_model": resolved,
            "persona_name": new_persona,
            "applies_to": "next call",
        })


# =============================================================================
# Worker subprocess prewarm — runs once when each worker child starts, BEFORE
# any call arrives. Loading Kokoro here (instead of inside entrypoint) means
# the 325 MB ONNX model is in memory when a job lands — the first response
# of every call is ~3-5s faster.
# =============================================================================


def prewarm(proc: JobProcess) -> None:
    log.info("Prewarming Kokoro TTS model...")
    # Cache loads any previously-saved entries from disk on construction —
    # so the greeting (and accumulated LLM phrasings) survive worker
    # restarts between calls.
    cache = TTSAudioCache(max_entries=500, persist_path=TTS_CACHE_PATH)
    # Treat the WAV dir as a manifest: any cache entry whose corresponding
    # WAV is missing gets dropped. That's the "git push delete" workflow —
    # user listens, deletes bad WAVs, runs tools/push_deletions.bat, next
    # call resynthesizes those phrases with current code.
    cache.validate_against_wav_dir(TTS_CACHE_WAV_DIR)
    voice = _resolve_voice()
    persona = _persona_for(voice)
    log.info("Using Kokoro voice: %s (persona=%s)", voice, persona)
    tts = KokoroTTS(
        model_path=str(KOKORO_MODEL),
        voices_path=str(KOKORO_VOICES),
        voice=voice,
        cache=cache,
    )
    log.info("Pre-rendering greeting chunks (skipped if already cached)...")
    for chunk in _greeting_chunks_for(persona):
        tts.prerender(chunk)
    # Ringback tone — synthesized from numpy (NOT via Kokoro), but stored
    # under the voice-aware cache key so on_enter's session.say() lookup
    # finds it. Inserted unconditionally on every prewarm so a stale
    # cache wipe or WAV-manifest invalidation can't break on_enter.
    cache.put(tts.cache_key(RINGBACK_CACHE_KEY), _generate_ringback_pcm())
    log.info("Cached ringback tone (%.1fs).", RINGBACK_DURATION_S)
    log.info("Kokoro TTS prewarmed; cache stats: %s", cache.stats())
    proc.userdata["kokoro_tts"] = tts


# =============================================================================
# Entrypoint — wired up at worker registration time
# =============================================================================


async def entrypoint(ctx: JobContext) -> None:
    log.info("Job received for room %s", ctx.room.name)
    await ctx.connect()

    # Re-read voice state file in case admin_set_voice was called by an
    # earlier call AFTER this worker finished prewarming. Without this,
    # voice switches don't take effect until two calls later (one to
    # write voice.txt, one to spawn a worker that prewarms with the new
    # value). With this, the switch applies on the very next call.
    tts: KokoroTTS = ctx.proc.userdata["kokoro_tts"]
    desired_voice = _resolve_voice()
    if desired_voice != tts._opts.voice:
        log.info(
            "Voice changed since prewarm: %s -> %s; switching + re-rendering greeting",
            tts._opts.voice, desired_voice,
        )
        tts.update_options(voice=desired_voice)
        # Synchronously re-render greeting in the new voice. ~5s blocking
        # cost on the call where voice just changed; subsequent calls have
        # this worker's prewarm picking up the right voice from voice.txt
        # so they're instant.
        new_persona = _persona_for(desired_voice)
        for chunk in _greeting_chunks_for(new_persona):
            await asyncio.to_thread(tts.prerender, chunk)
        # Ringback tone is voice-independent (a tone, not speech) but the
        # cache key is voice-prefixed, so the new voice needs its own
        # entry. Re-insert from numpy — same audio bytes, just under the
        # new voice's key.
        tts._cache.put(tts.cache_key(RINGBACK_CACHE_KEY), _generate_ringback_pcm())

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
        # Reuse the KokoroTTS instance prewarmed at worker subprocess start
        # (see prewarm() above). Avoids the 3-5s per-call model-load cost.
        tts=ctx.proc.userdata["kokoro_tts"],
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

    started_at = datetime.now()

    # Per-call event timeline. Every interesting framework event gets pushed
    # here with an elapsed-seconds timestamp from call start, then dumped to
    # the transcript JSON at shutdown. Reading the array in order tells you
    # exactly where time went: STT latency, LLM TTFT, tool-call duration,
    # TTS synthesis, post-tool-call dead time.
    events: list[dict] = []

    def _record(event_type: str, **fields) -> None:
        events.append({
            "t": round((datetime.now() - started_at).total_seconds(), 3),
            "event": event_type,
            **fields,
        })

    # Metrics from the framework's STT / LLM / TTS instrumentation. Includes
    # TTFT, duration, token counts (including cache hits — important for
    # verifying prompt caching is working).
    @session.on("metrics_collected")
    def _on_metrics(ev) -> None:
        m = getattr(ev, "metrics", None)
        if m is None:
            return
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
            _record(f"metrics.{kind}", **attrs)

    # Agent state transitions: listening (waiting for user) -> thinking
    # (LLM running) -> speaking (TTS playing). Gaps between these tell us
    # where the wall-clock time is being spent.
    @session.on("agent_state_changed")
    def _on_agent_state(ev) -> None:
        new_state = getattr(ev, "new_state", None) or getattr(ev, "state", None)
        old_state = getattr(ev, "old_state", None)
        log.info("agent_state %s -> %s", old_state, new_state)
        _record("agent_state", state=str(new_state), prev=str(old_state) if old_state else None)

    @session.on("user_state_changed")
    def _on_user_state(ev) -> None:
        new_state = getattr(ev, "new_state", None) or getattr(ev, "state", None)
        _record("user_state", state=str(new_state))

    # STT finalization — useful for measuring "STT finalize -> LLM start" gap.
    @session.on("user_input_transcribed")
    def _on_user_input(ev) -> None:
        text = getattr(ev, "transcript", None) or getattr(ev, "text", None)
        is_final = getattr(ev, "is_final", None)
        if is_final is False:
            return  # skip interim transcripts to avoid noise
        _record("user_input_transcribed", text=text)

    # Each item the framework adds to session.history (user msg, assistant
    # msg, tool call, tool output). Lets us correlate timeline with content.
    @session.on("conversation_item_added")
    def _on_item_added(ev) -> None:
        item = getattr(ev, "item", None)
        if item is None:
            return
        kind = type(item).__name__
        info: dict = {"item_type": kind}
        for attr in ("role", "name", "call_id", "interrupted"):
            v = getattr(item, attr, None)
            if v is not None:
                info[attr] = v
        # Truncate content to keep timeline readable; full content is in `items`.
        content = getattr(item, "content", None) or getattr(item, "output", None)
        if isinstance(content, list):
            content = " ".join(getattr(b, "text", str(b)) for b in content)
        if content:
            info["preview"] = (str(content)[:120] + "...") if len(str(content)) > 120 else str(content)
        _record("conversation_item_added", **info)

    # Tool execution boundaries — confirms the post-tool-call dead time we've
    # been hunting (LLM call after tool result returning sometimes waits ~7s).
    @session.on("function_tools_executed")
    def _on_tools_executed(ev) -> None:
        calls = getattr(ev, "function_calls", None) or []
        names = [getattr(c, "name", None) for c in calls]
        _record("function_tools_executed", names=names, count=len(names))

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

            # Snapshot of the per-worker TTS audio cache. Cumulative across
            # every call this worker has handled; lets us see whether
            # auto-caching is actually hitting on the LLM's natural
            # phrasings, or whether we need to tune the prompt to enforce
            # specific canned wording.
            tts = ctx.proc.userdata.get("kokoro_tts")
            cache_stats = tts.cache_stats() if tts is not None else None

            transcript = {
                "room": ctx.room.name,
                "caller_phone": caller_phone,
                "started_at": started_at.isoformat(),
                "ended_at": datetime.now().isoformat(),
                "duration_seconds": (datetime.now() - started_at).total_seconds(),
                "item_count": len(items),
                "event_count": len(events),
                "tts_cache_stats": cache_stats,
                "events": events,
                "items": items,
            }
            out.write_text(json.dumps(transcript, indent=2, default=str))
            log.info("Transcript written: %s (%d items)", out, len(items))
        except Exception:
            # Never let transcript writing crash session shutdown.
            log.exception("Failed to write transcript")

    async def save_tts_cache() -> None:
        """Persist the TTS audio cache to disk so subsequent worker
        subprocesses (and post-deploy restarts) start with the same
        entries already warm.

        Also dump each cached entry as a WAV file in the recordings
        subfolder so they sync to the Windows side and can be listened
        to for pronunciation review.
        """
        try:
            tts = ctx.proc.userdata.get("kokoro_tts")
            if tts is None:
                return
            await asyncio.to_thread(tts._cache.save)
            count = await asyncio.to_thread(
                tts._cache.dump_to_wav_dir, TTS_CACHE_WAV_DIR
            )
            log.info(
                "Dumped %d cached phrases as WAV to %s",
                count, TTS_CACHE_WAV_DIR,
            )
        except Exception:
            log.exception("Failed to save TTS cache")

    ctx.add_shutdown_callback(write_transcript)
    ctx.add_shutdown_callback(save_tts_cache)

    await session.start(
        room=ctx.room,
        agent=IrisAgent(
            caller_phone=caller_phone,
            persona=_persona_for(tts._opts.voice),
            room=ctx.room,
        ),
    )


if __name__ == "__main__":
    # num_idle_processes=1: only one warm worker subprocess pre-loaded with
    # Kokoro. Concurrent calls (rare at Lighthouse Inn) will cold-start a
    # second worker on demand. Keeps droplet memory footprint reasonable.
    agents.cli.run_app(agents.WorkerOptions(
        entrypoint_fnc=entrypoint,
        prewarm_fnc=prewarm,
        num_idle_processes=1,
    ))
