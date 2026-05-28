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

# StopResponse: raised from an Agent's on_user_turn_completed hook to stop
# the framework from invoking the LLM for that turn. Used by IrisAgent's
# silent (immediate-transfer) mode. Import path has wandered across
# livekit-agents minor versions; try a few before giving up. If the class
# can't be imported, we fall back to audio-output muting alone, which is
# also belt-and-suspenders-correct for the silent mode.
StopResponse = None
for _p in (
    "livekit.agents.voice.agent_activity",
    "livekit.agents.voice",
    "livekit.agents",
):
    try:
        StopResponse = getattr(__import__(_p, fromlist=["StopResponse"]), "StopResponse")
        break
    except (ImportError, AttributeError):
        continue
del _p

import inn_info
from audio_cache import TTSAudioCache
from intent_cache import DEFAULT_CACHE as INTENT_CACHE, IntentCallState
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

# Warm-transfer destinations. Each entry is (call_to, label, trunk_id):
#   - call_to: SIP user (for SIP-Domain trunk) or E.164 number (PSTN trunk)
#   - label: friendly name the LLM uses when announcing the transfer
#   - trunk_id: which trunk to dial through; resolved from env at import
#
# Routing rationale:
#   - front_desk -> SIP Domain trunk to ring the HT802 (via TwiML Bin
#     "HT802 Outbound Caller ID" on the SIP Domain's Voice URL — direct
#     INVITEs without TwiML get 403 from Twilio).
#   - eric -> PSTN trunk to Eric's cell.
#
# Why warm-bridge (create_sip_participant) instead of SIP REFER:
#   The LiveKit-side recording captures the room's audio. With warm-bridge,
#   Iris stays in the room while the human takes over, so the recording
#   captures the full conversation — including the human-handled portion.
#   That's invaluable right now for iterating on the LLM prompt. With REFER,
#   Iris drops at the moment Twilio accepts the REFER, ending the recording.
#
# Trade-off: the destination sees the trunk's authorized DID (+15419915071)
# as caller-ID, not the original caller's number. Revisit once the prompt
# is stable enough that recording every transfer is no longer essential.
TRANSFER_TARGETS: dict[str, tuple[str, str, str]] = {
    # LLM-facing destinations. The LLM only sees these two in its tool
    # docstring and chooses between them based on caller intent.
    "front_desk": ("frontdesk",    "the front desk", FRONTDESK_TRUNK_ID),
    "eric":       ("+15412286786", "Eric",           OUTBOUND_TRUNK_ID),
    # Internal-only destination: production port (HT802 FXS Port 2,
    # registered as `frontdesk2`). Used by the immediate-transfer code
    # path in on_enter when the caller dialed IMMEDIATE_TRANSFER_DID.
    # Not exposed to the LLM — the LLM should keep using "front_desk"
    # for guest-requested transfers, which lands at port 1 (dev).
    "front_desk_port2": ("frontdesk2", "the front desk", FRONTDESK_TRUNK_ID),
}

# Max time to wait for the destination to pick up before treating as
# no-answer and returning to the LLM so it can try the fallback.
TRANSFER_RING_TIMEOUT_S = 30

# Cloudbeds' platform Stripe publishable key — public by design (it's embedded
# in the Cloudbeds dashboard's iframe src and in our own portal_card.py). When
# we POST to Stripe /v1/tokens with this key as Basic-auth username, the
# resulting `tok_xxx` lives on the property's Stripe Connect account
# (acct_1RYA4xEJ572tmEoR), which is what dashboard_save_credit_card expects.
# Confirmed working via backend/scripts/test_stripe_pk_tokenize.py (5/23).
_CLOUDBEDS_PLATFORM_PK = (
    "pk_live_51GxYvfCkb5UaC5yLKjotmnTBp7MYbmiTqeNvDluaevZJ7xSsbL7RC4f3ZQdglMa9IVY6iPkpfDCdSJGrgdiyvuRo00jZpsTHkv"
)


async def _stripe_tokenize(
    pan: str,
    exp_month: int,
    exp_year: int,
    cvc: str,
    *,
    holder_name: str = "",
) -> dict:
    """Tokenize a card via Stripe /v1/tokens using Cloudbeds' platform
    publishable key. Returns {success, token_id, token_card} or
    {success: False, error, stripe_code}.

    PCI note: this is the only place raw PAN/CVC leave the agent process
    boundary. The HTTPS POST to api.stripe.com is end-to-end encrypted and
    Stripe receives nothing else from us. Caller MUST scrub `pan` and `cvc`
    from local variables immediately after this returns, regardless of
    success/failure.
    """
    form = {
        "card[number]":    pan,
        "card[exp_month]": str(exp_month),
        "card[exp_year]":  str(exp_year),
        "card[cvc]":       cvc,
    }
    if holder_name:
        form["card[name]"] = holder_name
    headers = {
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://api.stripe.com/v1/tokens",
                data=form,
                headers=headers,
                auth=(_CLOUDBEDS_PLATFORM_PK, ""),
            )
    except httpx.TimeoutException:
        return {"success": False, "error": "Stripe tokenize timed out.", "stripe_code": "timeout"}
    except httpx.HTTPError as e:
        return {"success": False, "error": f"Stripe HTTP error: {e}", "stripe_code": "http_error"}

    try:
        body = resp.json()
    except ValueError:
        return {"success": False, "error": "Stripe returned non-JSON.", "stripe_code": "non_json"}

    if resp.status_code != 200:
        err_obj = body.get("error") or {}
        err = err_obj.get("message") or "Stripe rejected the request"
        err_code = err_obj.get("code") or "unknown"
        # IMPORTANT: do not log `body` directly — Stripe error responses
        # sometimes echo back a `param` field that references which card
        # field was bad, but never the value itself. The message + code
        # alone are safe to log.
        log.warning("Stripe tokenize failed: status=%s code=%s msg=%s",
                    resp.status_code, err_code, err)
        return {"success": False, "error": err, "stripe_code": err_code}

    return {
        "success": True,
        "token_id": body.get("id"),
        "token_card": body.get("card") or {},
    }


# Spoken when a transfer attempt does NOT connect (no_answer or exception)
# and the conversation needs to recover gracefully. Used in two places that
# both need to offer a deterministic next step to the caller:
#   1. on_enter silent-mode (Port 2) escalation after immediate-transfer
#      fails — un-mutes Iris and falls into conversational mode.
#   2. on_user_turn_completed cache-driven transfer (speak_to_human intent)
#      after transfer_to fails — keeps the call conversational.
# Sentence-split chunks are added to PERSISTENT_OPENERS so the first
# sentence plays as a cache hit (audio starts ~0.3s after the failure
# instead of waiting on Kokoro synthesis).
TRANSFER_FALLBACK_PHRASE = (
    "I apologize. The front desk isn't picking up. "
    "I can take a message, or I can try Eric's cell. "
    "Which would you prefer?"
)

# Dual-DID setup: the hotel owns two sequential Twilio DIDs.
#   +15419915071 (the "dev" / "Iris-engages" line):  Iris answers normally,
#       runs through the full prompt flow, can engage the caller and use
#       all tools. Used for development and capability exploration.
#   +15419915070 (the "immediate-transfer" / "production" line, IMMEDIATE_TRANSFER_DID):
#       on inbound, Iris immediately bridges the caller to the front desk
#       with no AI greeting. Recording still captures all legs of the
#       conversation, which is what feeds prompt development with real
#       customer-to-front-desk interactions.
# Both DIDs route to the same Twilio Elastic SIP Trunk and the same LiveKit
# inbound trunk + dispatch rule. The agent branches behavior in `on_enter`
# based on which DID the call arrived on (read from SIP participant
# attributes).
IMMEDIATE_TRANSFER_DID = "+15419915070"
# Which TRANSFER_TARGETS entry the immediate-transfer mode routes to.
# Currently `front_desk_port2` (HT802 FXS Port 2 = production phone).
IMMEDIATE_TRANSFER_DESTINATION = "front_desk_port2"

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

# Phrases to pre-render at worker prewarm. The goal: hit the TTS cache on
# the FIRST sentence of an Iris response, which gates start-of-audio. Once
# the first sentence plays, Kokoro can synthesize the rest in parallel.
#
# Curated from frequency analysis of ~77 transcripts (May 10-13, 2026):
# every entry below appeared verbatim in at least two real calls, OR is a
# system-controlled phrase (greeting, transfer status, voice admin) that
# Iris emits directly without LLM phrasing variability.
#
# The cache persists to disk across worker / service / droplet restarts
# (~/.cache/iris/tts_cache.pkl), so this prewarm pass is a one-time cost
# on each new deploy; subsequent worker starts skip already-cached entries.
PERSISTENT_OPENERS: tuple[str, ...] = (
    # ----- Acknowledgments / fillers (high-frequency sentence starters) -----
    "Of course.",
    "Sure.",
    "Got it.",
    "Thank you.",
    "One moment.",
    "Let me check that for you.",
    "I apologize.",
    "You're right.",
    "Sorry, I didn't catch that.",
    "I'm sorry, I didn't quite catch that.",
    "Could you say that again?",
    # ----- Transfer flow (system-controlled exact strings) -----
    "Let me transfer you to the front desk now.",
    "Of course. Let me transfer you to the front desk now.",
    "Sure, let me transfer you to the front desk now.",
    "Okay, let me transfer you to the front desk.",
    "Let me connect you to the front desk now.",
    "Connecting you to the front desk now.",
    "Connecting you to Eric now, one moment.",
    "You're connected — I'll step out.",
    "The front desk isn't picking up — let me try Eric's cell. One moment.",
    "Eric's not picking up. Would you like me to try the front desk?",
    # Sentence-split components of TRANSFER_FALLBACK_PHRASE — spoken when
    # a transfer attempt fails (Port 2 silent-mode escalation or the
    # speak_to_human post-action no-answer path). Restructured 5/17 into
    # four short sentences (was three with a long comma-joined first
    # sentence) after a test heard a mid-word stutter inside "front" —
    # shorter chunks cache and stream more cleanly under CPU pressure.
    # "I apologize." is reused from the existing acknowledgments block above.
    "The front desk isn't picking up.",
    "I can take a message, or I can try Eric's cell.",
    "Which would you prefer?",
    # ----- Pet policy (highest-frequency hotel-fact answers) -----
    "Yes, dogs are welcome!",
    "Yes, we do allow dogs.",
    "Yes, you can bring a dog.",
    "Yes, we welcome dogs with a $20 fee per stay.",
    "The pet fee is $20 per stay for dogs.",
    "I'm sorry, but we don't accept cats.",
    # ----- Hotel facts (frequent inn_info answers) -----
    "Check-in is from 2 PM to 8 PM.",
    "Check-out is at 11 AM.",
    "We'd love to have you.",
    # ----- Sign-off -----
    "Is there anything else I can help you with today?",
    "If you have any questions or need to make changes, please call us.",
    # ----- Voice admin (system-controlled, exact strings) -----
    "Voice set to Iris. It will apply to your next call.",
    "Voice set to Henry. It will apply to your next call.",
    "Voice set to Aoede. It will apply to your next call.",
    "Voice set to Eric. It will apply to your next call.",
)


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
# Was 1.5s; shortened to 0.8s on 5/17 so the residual ringback playback
# after a transfer connects is bounded to ~0.8s in the worst case where
# SpeechHandle.interrupt() doesn't stop in-flight audio with
# allow_interruptions=False. Combined with the 3.2s pause below, the
# total ring cycle is 4s — close enough to the standard US ringback
# cadence (2s on / 4s off) that callers still recognize it as ringing.
RINGBACK_DURATION_S = 0.8


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
        called_number: str | None = None,
    ) -> None:
        self._is_admin = bool(ADMIN_PHONE) and caller_phone == ADMIN_PHONE
        self._persona = persona
        # AgentSession in livekit-agents 1.5.8 doesn't expose `room` as a
        # public attribute, so we capture ctx.room here. transfer_to needs
        # the room name to dial an outbound SIP participant into it.
        self._room = room
        # The DID the caller actually dialed (e.g. +15419915070 vs
        # +15419915071). Used to switch into immediate-transfer mode on
        # the production DID — see on_enter.
        self._called_number = called_number
        # Silent mode: when the call arrived on the immediate-transfer DID,
        # Iris must NOT generate any responses for the rest of the call.
        # The previous version of the code only skipped the greeting in
        # on_enter, but the LLM still fired on subsequent STT events and
        # Iris's TTS audio leaked into the bridged human conversation
        # between caller and front desk. Two-layer fix: (1) disable audio
        # output via session.output.set_audio_enabled(False) in on_enter,
        # and (2) override on_user_turn_completed to short-circuit the
        # LLM-invocation pipeline via StopResponse.
        self._silent = (called_number == IMMEDIATE_TRANSFER_DID)
        # Intent-cache state: per-call. The cache fires on common static-fact
        # questions (pet fee, check-in time, etc.) so the LLM is skipped on
        # the easy turns. State tracks which response variants we've already
        # used (so we don't immediately repeat) and whether the cache has
        # been disabled for this call (after any tool call fires, we defer
        # to the LLM for the rest of the conversation).
        self._intent_state = IntentCallState()
        super().__init__(instructions=build_system_prompt(
            caller_phone=caller_phone,
            is_admin=self._is_admin,
            persona=persona,
        ))
        self._caller_phone = caller_phone
        if self._is_admin:
            log.info("Caller %s recognized as admin", caller_phone)
        log.info(
            "Agent init: caller=%s called=%s persona=%s admin=%s silent=%s",
            caller_phone, called_number, persona, self._is_admin, self._silent,
        )

    async def on_enter(self) -> None:
        # Brief wait for the SIP audio bridge to settle. Without it the
        # first audio plays into a void on some carriers.
        await asyncio.sleep(0.3)

        # Immediate-transfer DID: if the call arrived at the production
        # number (+15419915070), skip the AI greeting entirely and bridge
        # the caller straight to the production front-desk phone (HT802
        # FXS Port 2 = `frontdesk2`). Iris stays silent in the room so
        # the recording still captures all legs of the human conversation
        # — that's the whole point: collect real customer-to-front-desk
        # interactions to drive prompt development on the dev DID.
        if self._silent:
            # PRODUCTION INCIDENT 2026-05-14/15: SIP transfer to frontdesk2
            # takes 11-16 seconds end-to-end (mostly waiting for the human
            # to walk over and pick up port 2). With Iris muted during that
            # window, callers heard total silence and concluded the line
            # was dead — most hung up within 13 seconds. Fix is the
            # verbal handoff below + the ringback loop inside transfer_to
            # which plays during the SIP ring wait.
            #
            # Pre-ringback removed 5/17: the synthetic tone before the
            # verbal handoff was burbling on real PSTN calls because it
            # hit the still-stabilizing SIP audio bridge ~0.3s after
            # answer. The handoff phrase alone is the meaningful cue;
            # the looping ringback inside transfer_to then takes over
            # once the bridge has settled. (RINGBACK_CACHE_KEY is still
            # generated/cached for the loop's use.)
            HANDOFF_PHRASE = "Connecting you to the front desk now."
            try:
                await self.session.say(HANDOFF_PHRASE, allow_interruptions=False)
                log.info("Silent mode: spoke verbal handoff")
            except Exception:
                log.exception("Could not speak handoff (continuing anyway)")
            # Early-mute removed 2026-05-17: muting here silenced the
            # ringback loop inside transfer_to during the SIP wait, so
            # callers heard the verbal handoff and then ~30s of dead air
            # before the Phase 2 escalation. Two newer mechanisms now
            # cover the original "Iris audio must not leak into the
            # bridged human conversation" concern: (1) on_user_turn_completed
            # short-circuits via StopResponse while self._silent is True,
            # so the LLM is never invoked during the ring or after the
            # connect; (2) transfer_to's connected branch calls
            # set_audio_enabled(False) deterministically once the human
            # picks up. Both are belt-and-suspenders against TTS leakage.
            log.info(
                "Immediate-transfer mode: called=%s -> %s (skipping greeting); room=%s",
                self._called_number, IMMEDIATE_TRANSFER_DESTINATION,
                self._room.name if self._room is not None else "<unknown>",
            )
            # Diagnostic instrumentation 2026-05-15: production incident
            # where the audio bridge sometimes fails to relay RTP for
            # external callers even when both legs are SIP-connected
            # (Twilio shows both legs "Completed"; caller and frontdesk2
            # both pick up; both hear silence). Wrap transfer_to() with
            # explicit timing + exception logging so docker logs from
            # the livekit-sip container can be correlated with Python-
            # side state.
            import time as _time
            _transfer_t0 = _time.monotonic()
            log.info("transfer_to: starting (dest=%s)", IMMEDIATE_TRANSFER_DESTINATION)
            _status: str = ""
            try:
                _transfer_result = await self.transfer_to(IMMEDIATE_TRANSFER_DESTINATION)
                _transfer_elapsed = _time.monotonic() - _transfer_t0
                log.info(
                    "transfer_to: returned (elapsed=%.2fs, result=%r)",
                    _transfer_elapsed, _transfer_result,
                )
                try:
                    _result_obj = json.loads(_transfer_result) if _transfer_result else {}
                except (json.JSONDecodeError, TypeError):
                    _result_obj = {}
                _status = _result_obj.get("status", "")
            except Exception as _transfer_exc:
                _transfer_elapsed = _time.monotonic() - _transfer_t0
                log.exception(
                    "transfer_to: EXCEPTION after %.2fs: %s",
                    _transfer_elapsed, _transfer_exc,
                )
                _status = "exception"

            if _status == "connected":
                # Transfer succeeded — destination joined the room and the
                # human leg is in control. Iris stays silent/muted; the
                # framework keeps the recording going via the egress.
                return

            # Phase 2 escalation: transfer did NOT connect (no_answer,
            # exception, or some other non-connect outcome). Without this
            # block, Iris would stay muted forever and the caller would
            # sit in silence — same failure mode as the original Port 2
            # incident, just one layer deeper. Un-mute, drop the silent
            # flag so subsequent user turns invoke the LLM, and speak a
            # graceful fallback that gives the caller an actionable next
            # step (message or Eric's cell).
            log.warning(
                "Silent-mode transfer did not connect (status=%r); "
                "escalating to conversational mode",
                _status,
            )
            try:
                self.session.output.set_audio_enabled(True)
                log.info("Silent mode: re-enabled session audio output")
            except Exception:
                log.exception("Could not re-enable audio output")
            # Flip the flag so on_user_turn_completed stops short-circuiting
            # the LLM. From this point the call behaves like a normal Port 1
            # conversation, just without the initial greeting.
            self._silent = False
            try:
                await self.session.say(TRANSFER_FALLBACK_PHRASE, allow_interruptions=True)
            except Exception:
                log.exception("Could not speak silent-mode escalation message")
            return

        # (Synthetic ringback tone removed 2026-05-13 — Twilio's PSTN side
        # is now providing real ringback before LiveKit answers, so the
        # synthesized one was layering on top. RINGBACK_CACHE_KEY and
        # _generate_ringback_pcm are still defined upstream as dead code
        # in case the silent-connect gap returns.)
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

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        """Two responsibilities, in priority order:

        1. **Silent mode (immediate-transfer DID)**: skip the LLM entirely.
           The framework's default pipeline would invoke the LLM after STT
           and the assistant's audio would leak into the bridged human
           conversation. Audio output is already muted via
           `session.output.set_audio_enabled(False)` in on_enter; this hook
           prevents the wasted LLM call + phantom assistant message in
           chat history. Raising `StopResponse` is the supported way.

        2. **Intent cache**: for normal calls, try to match the STT text
           to a static-fact intent (pet fee, check-in time, WiFi, etc.).
           On a hit, pick a response variant, speak it via `session.say()`,
           and `raise StopResponse` to skip the LLM for this turn. On a
           miss, return normally and let the LLM handle the turn.
        """
        # Extract user text once (used by both paths).
        text = getattr(new_message, "text_content", None)
        if text is None and hasattr(new_message, "content"):
            text = new_message.content
        if isinstance(text, list):
            text = " ".join(
                getattr(b, "text", str(b)) for b in text if b is not None
            )
        text = (text or "").strip()

        # Path 1: silent mode — short-circuit unconditionally.
        if self._silent:
            log.info("Silent mode: skipping LLM for user turn: %r", text)
            if StopResponse is not None:
                raise StopResponse()
            return

        # Track turn depth for the `skip_after_turn` guardrail.
        self._intent_state.user_turn_count += 1

        # If any tool has been called this conversation, disable the cache
        # for the rest of the call — we're inside a caller-specific flow.
        if not self._intent_state.disabled:
            try:
                for item in turn_ctx.items:
                    if type(item).__name__ == "FunctionCall":
                        self._intent_state.disabled = True
                        log.info(
                            "Intent cache disabled for rest of call "
                            "(tool was called earlier)"
                        )
                        break
            except Exception:
                # `turn_ctx.items` API might differ across versions; safe
                # to skip the check — worst case is a cache hit during a
                # flow, which the guardrails should also catch.
                pass

        # Path 2: intent cache classify + speak.
        if not text:
            return
        intent_id = INTENT_CACHE.classify(text, self._intent_state)
        if intent_id is None:
            return
        chosen = INTENT_CACHE.pick_response(
            intent_id,
            persona=self._persona,
            exclude_texts=self._intent_state.used_response_texts,
        )
        if not chosen:
            return

        log.info(
            "Intent cache HIT: intent=%s, response=%r (turn %d)",
            intent_id, chosen[:80], self._intent_state.user_turn_count,
        )
        self._intent_state.used_response_texts.add(chosen)
        try:
            # session.say() adds the assistant message to chat_ctx and
            # plays the audio. The text is in the TTS cache (prewarmed),
            # so playback starts ~300ms after this call.
            await self.session.say(chosen, allow_interruptions=True)
        except Exception:
            log.exception(
                "session.say() failed for cached intent %s; falling back to LLM",
                intent_id,
            )
            return  # let the LLM handle it

        # Optional post-action: e.g. speak_to_human's response is "Let me
        # transfer you" — the canned text is just the verbal handoff; the
        # actual transfer fires here. Disable the cache for the rest of the
        # call so subsequent STT turns during/after the transfer don't try
        # to classify on top of an in-flight or completed bridge.
        post_action = INTENT_CACHE.get_post_action(intent_id)
        if post_action == "transfer_to_front_desk":
            self._intent_state.disabled = True
            await self._execute_transfer_with_fallback("front_desk")
        elif post_action:
            log.warning(
                "Intent %s declared unknown post_action %r — ignoring",
                intent_id, post_action,
            )

        if StopResponse is not None:
            raise StopResponse()

    async def _execute_transfer_with_fallback(self, destination: str) -> None:
        """Trigger a warm transfer and handle the outcome deterministically.

        On `connected`: mute Iris's audio output and set self._silent so
        subsequent STT turns short-circuit before the LLM — the human leg
        is now driving the conversation and Iris must not interject.

        On any non-connected outcome (no_answer, exception, malformed
        response): leave Iris conversational and speak TRANSFER_FALLBACK_PHRASE
        so the caller has an explicit next step (message or Eric's cell).

        While the transfer is ringing, self._silent is set to True so any
        STT-triggered user turns during the wait don't speak on top of the
        ringback loop running inside transfer_to. The flag is restored to
        its prior value on failure.
        """
        prev_silent = self._silent
        # Silence the call during the ring wait. transfer_to's background
        # ringback loop runs unaffected — it's the only audio that should
        # play between now and either pickup or no_answer.
        self._silent = True
        status = ""
        try:
            result_json = await self.transfer_to(destination)
            try:
                result_obj = json.loads(result_json) if result_json else {}
            except (json.JSONDecodeError, TypeError):
                result_obj = {}
            status = result_obj.get("status", "")
        except Exception:
            log.exception("transfer_to(%s) from intent post-action raised", destination)
            status = "exception"

        if status == "connected":
            log.info(
                "Post-action transfer to %s connected; muting Iris for the rest of the call",
                destination,
            )
            try:
                self.session.output.set_audio_enabled(False)
            except Exception:
                log.exception("Could not mute audio after connected transfer")
            # Leave self._silent = True so future user turns skip the LLM.
            return

        log.warning(
            "Post-action transfer to %s did not connect (status=%r); offering fallback",
            destination, status,
        )
        # Restore conversational mode so the LLM (and intent cache where
        # appropriate) can handle the caller's next response.
        self._silent = prev_silent
        try:
            await self.session.say(
                TRANSFER_FALLBACK_PHRASE, allow_interruptions=True,
            )
        except Exception:
            log.exception("Could not speak transfer fallback phrase")

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

        target = TRANSFER_TARGETS.get(destination)
        if target is None:
            return json.dumps({
                "error": f"Unknown destination: {destination!r}",
                "valid_destinations": list(TRANSFER_TARGETS),
            })

        sip_to, label, trunk_id = target
        if not trunk_id:
            log.error(
                "Transfer to %s requested but trunk env var is empty",
                destination,
            )
            return json.dumps({
                "error": f"{destination} routing is not configured on the agent.",
            })
        if self._room is None:
            log.error("Transfer requested but agent has no room reference")
            return json.dumps({"error": "Internal: no room available."})
        room_name = self._room.name
        log.info(
            "Warm-bridge transfer: %s -> %s via %s (room=%s)",
            destination, sip_to, trunk_id, room_name,
        )

        # Background ringback during the SIP ring wait. Same rationale as
        # the silent-mode (Port 2) fix: callers will hang up after ~13s of
        # dead air on what feels like a dropped call. Looping the cached
        # ringback (1.5s tone + ~3.5s pause) approximates US ring cadence
        # (2s on / 4s off) so the caller hears the line is alive while the
        # destination phone rings.
        async def _ringback_loop():
            # add_to_chat_ctx=False keeps the "__ringback_tone__" sentinel
            # out of the LLM's chat history. Without it, every ring would
            # show up as an assistant message and could confuse subsequent
            # LLM turns. TypeError fallback handles livekit-agents versions
            # that predate the kwarg.
            #
            # SpeechHandle tracking + interrupt() is the fix for the
            # "dial tone continued for a moment after I answered" issue:
            # without it, the in-flight session.say() audio plays through
            # to completion (up to RINGBACK_DURATION_S seconds) even after
            # the destination picks up, because allow_interruptions=False
            # blocks user-driven interruption AND awaiting an async-cancel
            # doesn't stop already-queued audio. handle.interrupt() does.
            handle = None
            try:
                while True:
                    try:
                        try:
                            handle = self.session.say(
                                RINGBACK_CACHE_KEY,
                                allow_interruptions=False,
                                add_to_chat_ctx=False,
                            )
                        except TypeError:
                            handle = self.session.say(
                                RINGBACK_CACHE_KEY,
                                allow_interruptions=False,
                            )
                    except Exception:
                        log.exception("Ringback say() failed; stopping loop")
                        return
                    try:
                        if hasattr(handle, "wait_for_playout"):
                            await handle.wait_for_playout()
                        else:
                            await handle
                    except asyncio.CancelledError:
                        if hasattr(handle, "interrupt"):
                            try:
                                handle.interrupt()
                            except Exception:
                                log.exception("Could not interrupt in-flight ringback")
                        raise
                    handle = None
                    # 3.2s gap + 0.8s tone above = 4s ring cycle, close to
                    # the standard US 2s-on/4s-off cadence.
                    await asyncio.sleep(3.2)
            except asyncio.CancelledError:
                # If we were cancelled while sleeping (between rings),
                # there's no in-flight audio to interrupt. If cancelled
                # during the await above, the inner handler already issued
                # the interrupt.
                raise

        ringback_task: asyncio.Task | None = None
        try:
            ringback_task = asyncio.create_task(_ringback_loop())
        except Exception:
            log.exception("Could not start ringback loop (continuing silently)")

        async def _stop_ringback() -> None:
            if ringback_task is None or ringback_task.done():
                return
            ringback_task.cancel()
            try:
                await ringback_task
            except (asyncio.CancelledError, Exception):
                pass

        try:
            lk = api.LiveKitAPI()
            try:
                await lk.sip.create_sip_participant(
                    api.CreateSIPParticipantRequest(
                        sip_trunk_id=trunk_id,
                        sip_call_to=sip_to,
                        room_name=room_name,
                        participant_identity=f"transfer-{destination}",
                        participant_name=label,
                        # play_dialtone=False: we play our own synthetic
                        # ringback in the background loop above. Letting
                        # LiveKit also play dialtone would double-stack
                        # tones on the caller's line.
                        play_dialtone=False,
                        wait_until_answered=True,
                        ringing_timeout=timedelta(seconds=TRANSFER_RING_TIMEOUT_S),
                    )
                )
            finally:
                await lk.aclose()
        except Exception:
            log.exception("Transfer to %s failed", destination)
            await _stop_ringback()
            return json.dumps({
                "status": "no_answer",
                "destination": destination,
                "display": label,
            })

        await _stop_ringback()
        log.info("Transfer to %s connected", destination)

        # Auto-mute Iris's audio output and set _silent so subsequent STT
        # turns short-circuit before the LLM. Without this, the LLM kept
        # getting invoked on each STT event during the human-to-human
        # conversation and, despite the [Transfer Scope Rules] prompt
        # instructing it to stop responding, eventually broke in with
        # phantom lines like "I'm sorry, I'm still on the line"
        # (observed in production testing 2026-05-17). The LLM may still
        # generate its prompt-mandated "You're connected — I'll step out"
        # line via the tool-return path, but with output muted the caller
        # never hears it — small UX loss for robust silencing. The egress
        # recording captures all room audio regardless of Iris's mute
        # state, so the human conversation is still preserved.
        try:
            self.session.output.set_audio_enabled(False)
            log.info("Connected transfer: muted Iris audio output")
        except Exception:
            log.exception("Could not mute audio output after connected transfer")
        self._silent = True

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

    @function_tool
    async def admin_dtmf_mute_test(self, duration_seconds: int = 20) -> str:
        """[Admin only] Gating test for the in-call DTMF card-capture build (task #12).

        Mutes the caller's audio track via the LiveKit server admin API for
        `duration_seconds` (clamped to 5-60) and logs every sip_dtmf_received
        event that fires during the muted window. Purpose: determine whether
        DTMF events keep arriving when the caller's audio track is muted. If
        yes, server-side mute is a viable mitigation for the egress-tone-leak
        problem we observed in OGG recordings. If no, the build needs to use
        stop/restart-egress as the mitigation instead.

        Returns JSON: {status, verdict, dtmf_events_received, digits_received,
        muted_for_seconds, interpretation}. After this returns, the admin
        will separately run detect_dtmf_in_ogg.py against the call's OGG to
        confirm the recording is clean.
        """
        if not self._is_admin:
            return json.dumps({"error": "Not authorized."})
        if self._room is None:
            return json.dumps({"error": "Internal: no room available."})

        duration = max(5, min(60, int(duration_seconds)))

        # Find the caller's remote participant (the SIP one with an audio track).
        caller_participant = None
        for ident, p in self._room.remote_participants.items():
            try:
                for pub in p.track_publications.values():
                    kind = getattr(pub, "kind", None)
                    if kind == rtc.TrackKind.KIND_AUDIO or "AUDIO" in str(kind).upper():
                        caller_participant = p
                        break
            except Exception:
                pass
            if caller_participant is not None:
                break
        if caller_participant is None:
            return json.dumps({
                "error": "No remote participant with an audio track found.",
                "remote_participant_count": len(self._room.remote_participants),
            })

        # Find the audio track SID
        audio_track_sid: str | None = None
        for pub in caller_participant.track_publications.values():
            kind = getattr(pub, "kind", None)
            if kind == rtc.TrackKind.KIND_AUDIO or "AUDIO" in str(kind).upper():
                audio_track_sid = getattr(pub, "sid", None)
                if audio_track_sid:
                    break
        if not audio_track_sid:
            return json.dumps({"error": "Audio track SID not found on caller participant."})

        log.info(
            "DTMF mute test: identity=%s track_sid=%s duration=%ds",
            caller_participant.identity, audio_track_sid, duration,
        )

        # Set up DTMF event collector — flag-gated so events received outside
        # the muted window don't pollute the result.
        import time as _time
        listening = [True]
        events: list[dict] = []

        def _on_dtmf(dtmf) -> None:
            if not listening[0]:
                return
            digit = getattr(dtmf, "digit", "?")
            code = getattr(dtmf, "code", -1)
            events.append({
                "t": round(_time.monotonic(), 3),
                "digit": str(digit),
                "code": int(code) if isinstance(code, (int, float)) else -1,
            })
            log.info("DTMF mute test: received digit=%s code=%s", digit, code)

        try:
            self._room.on("sip_dtmf_received", _on_dtmf)
        except Exception:
            log.exception("DTMF mute test: could not register handler")
            return json.dumps({"error": "Could not register DTMF handler."})

        # Mute via admin API
        mute_t0 = _time.monotonic()
        try:
            lk = api.LiveKitAPI()
            try:
                await lk.room.mute_published_track(
                    api.MuteRoomTrackRequest(
                        room=self._room.name,
                        identity=caller_participant.identity,
                        track_sid=audio_track_sid,
                        muted=True,
                    )
                )
            finally:
                await lk.aclose()
        except Exception as e:
            log.exception("DTMF mute test: failed to mute track")
            listening[0] = False
            try:
                self._room.off("sip_dtmf_received", _on_dtmf)
            except Exception:
                pass
            return json.dumps({"error": f"Could not mute caller track: {e}"})

        log.info("DTMF mute test: muted, sleeping %ds", duration)
        await asyncio.sleep(duration)

        # Unmute
        unmute_ok = False
        try:
            lk = api.LiveKitAPI()
            try:
                await lk.room.mute_published_track(
                    api.MuteRoomTrackRequest(
                        room=self._room.name,
                        identity=caller_participant.identity,
                        track_sid=audio_track_sid,
                        muted=False,
                    )
                )
                unmute_ok = True
            finally:
                await lk.aclose()
        except Exception:
            log.exception("DTMF mute test: failed to UN-mute track (caller may stay muted!)")

        # Stop collecting; clean up the handler
        listening[0] = False
        try:
            self._room.off("sip_dtmf_received", _on_dtmf)
        except Exception:
            log.exception("DTMF mute test: could not remove handler (harmless)")

        elapsed = _time.monotonic() - mute_t0
        digits = [e["digit"] for e in events]
        log.info(
            "DTMF mute test complete: muted=%.1fs events=%d digits=%s unmute_ok=%s",
            elapsed, len(events), digits, unmute_ok,
        )

        if events:
            verdict = "M1_VIABLE"
            interpretation = (
                f"{len(events)} DTMF event(s) fired while the audio track was muted. "
                "Server-side track mute does NOT block sip_dtmf_received delivery, "
                "so M1 (mute during capture) is a viable mitigation."
            )
        else:
            verdict = "M1_INCONCLUSIVE_OR_BLOCKED"
            interpretation = (
                "Zero DTMF events received while muted. Either (a) admin pressed no "
                "keys during the window, or (b) DTMF delivery is suppressed alongside "
                "the audio track, making M1 unusable. Re-run with deliberate key presses; "
                "if still zero, fall back to M2 (stop/restart egress)."
            )

        return json.dumps({
            "status": "complete",
            "verdict": verdict,
            "muted_for_seconds": round(elapsed, 1),
            "unmute_ok": unmute_ok,
            "dtmf_events_received": len(events),
            "digits_received": digits,
            "interpretation": interpretation,
        })

    @function_tool
    async def capture_card_dtmf(self, reservation_id: str) -> str:
        """Capture a credit card from the caller via DTMF keypad entry and
        attach it to a Cloudbeds reservation. Hands-free — the caller never
        leaves the call.

        Flow (entirely in-call):
          1. Resolve caller's audio track sid and look up the reservation's
             internal booking_id.
          2. Mute the caller's audio track via LiveKit server admin API.
             This keeps DTMF tones out of the egress recording. sip_dtmf
             events keep firing (separate signaling channel).
          3. For each of {card number, expiration MMYY, security code}:
             prompt the caller, accumulate digits as they're pressed, end
             stage on `#`, abort stage on `*`. Re-prompt once on timeout.
          4. Tokenize the collected card via Stripe /v1/tokens with
             Cloudbeds' platform publishable key. Stripe returns a
             tok_xxx on the property's connected account.
          5. Scrub PAN/CVC from local variables (PCI).
          6. POST the token + token_card to the backend's
             /save_card_via_token route, which calls Cloudbeds dashboard
             save_credit_card to attach the card.
          7. Un-mute the caller, return result JSON.

        Returns JSON with `status` in:
          success — card attached. Briefly confirm to the caller ("Got it,
                    that's Visa ending 4-9-5-8 on file").
          declined — Stripe rejected the card (bad number/CVC/expiry,
                     declined-by-issuer). Offer to retry or fall back to
                     the call-front-desk path.
          timeout — caller stopped pressing keys mid-capture. Same fallback.
          aborted_by_user — caller pressed `*`. Acknowledge ("Okay, I've
                            cancelled that") and offer the fallback.
          error — internal failure (mute API, booking lookup, save failure,
                  etc.). Apologize and offer the call-front-desk fallback.

        Do NOT call this tool a second time on the same call if a previous
        capture returned `declined` or `error` — those need a human-mediated
        decision, not silent retry. Offer the caller a choice instead.
        """
        if self._room is None:
            return json.dumps({"status": "error", "error": "Internal: no room."})
        reservation_id = (reservation_id or "").strip()
        if not reservation_id:
            return json.dumps({"status": "error", "error": "reservation_id is required."})

        # ---- 1. Locate caller's audio track ----
        caller_participant = None
        for ident, p in self._room.remote_participants.items():
            try:
                for pub in p.track_publications.values():
                    kind = getattr(pub, "kind", None)
                    if kind == rtc.TrackKind.KIND_AUDIO or "AUDIO" in str(kind).upper():
                        caller_participant = p
                        break
            except Exception:
                pass
            if caller_participant is not None:
                break
        if caller_participant is None:
            return json.dumps({
                "status": "error",
                "error": "Caller audio track not found in the room.",
            })

        audio_track_sid: str | None = None
        for pub in caller_participant.track_publications.values():
            kind = getattr(pub, "kind", None)
            if kind == rtc.TrackKind.KIND_AUDIO or "AUDIO" in str(kind).upper():
                audio_track_sid = getattr(pub, "sid", None)
                if audio_track_sid:
                    break
        if not audio_track_sid:
            return json.dumps({
                "status": "error",
                "error": "Caller audio track has no SID.",
            })

        # ---- 2. DTMF state machine setup ----
        STAGES: tuple[tuple[str, str], ...] = (
            (
                "pan",
                "Please enter your card number, then press pound.",
            ),
            (
                "exp",
                (
                    "Got it. Now the expiration as four digits — "
                    "month month year year. For example, October "
                    "twenty-twenty-eight would be 1, 0, 2, 8. "
                    "Then press pound."
                ),
            ),
            (
                "cvc",
                (
                    "Got it. Now your three or four digit security code, "
                    "then pound."
                ),
            ),
        )
        STAGE_TIMEOUT_S = 30
        MAX_DIGITS_BY_STAGE = {"pan": 19, "exp": 4, "cvc": 4}

        buffers: dict[str, str] = {"pan": "", "exp": "", "cvc": ""}
        stage_index = [0]  # mutable for closure
        stage_complete = asyncio.Event()
        stage_aborted = asyncio.Event()

        # RFC 4733 / LiveKit SipDTMF convention (verified empirically on
        # the M1 mute test, 5/28): digit is the printable character,
        # code is the RFC 4733 numeric value. For 0-9 these are 0-9 and
        # "0"-"9". For * it's code=10 / digit="*". For # it's code=11 /
        # digit="#". A-D map to 12-15 / "A"-"D" but are rare on consumer
        # keypads. We accept both the digit string AND the code as the
        # source of truth so we don't get wedged if either side reports
        # the other format. Diagnostic logging on EVERY event so we can
        # tell from journalctl what each press looked like (PCI-safe:
        # only the digit/code is logged, never accumulated buffer state).
        def _on_dtmf(dtmf) -> None:
            try:
                stage_name = STAGES[stage_index[0]][0]
            except IndexError:
                return
            d_raw = getattr(dtmf, "digit", "")
            c_raw = getattr(dtmf, "code", -1)
            d_str = str(d_raw or "").strip()
            try:
                c_int = int(c_raw) if c_raw is not None else -1
            except (TypeError, ValueError):
                c_int = -1
            log.info(
                "capture_card_dtmf[%s]: received digit=%r code=%s",
                stage_name, d_str, c_int,
            )
            # Normalize: figure out what key was pressed.
            is_pound = d_str == "#" or c_int == 11
            is_star = d_str == "*" or c_int == 10
            is_digit_0_9 = d_str in "0123456789" or (0 <= c_int <= 9)
            if is_pound:
                stage_complete.set()
            elif is_star:
                stage_aborted.set()
            elif is_digit_0_9:
                if d_str in "0123456789":
                    digit_char = d_str
                elif 0 <= c_int <= 9:
                    digit_char = str(c_int)
                else:
                    return
                if len(buffers[stage_name]) < MAX_DIGITS_BY_STAGE[stage_name]:
                    buffers[stage_name] += digit_char

        try:
            self._room.on("sip_dtmf_received", _on_dtmf)
        except Exception:
            log.exception("capture_card_dtmf: could not register DTMF handler")
            return json.dumps({"status": "error", "error": "Could not register handler."})

        # ---- 3. Mute caller's track + freeze STT ----
        async def _set_mute(muted: bool) -> bool:
            try:
                lk = api.LiveKitAPI()
                try:
                    await lk.room.mute_published_track(
                        api.MuteRoomTrackRequest(
                            room=self._room.name,
                            identity=caller_participant.identity,
                            track_sid=audio_track_sid,
                            muted=muted,
                        )
                    )
                    return True
                finally:
                    await lk.aclose()
            except Exception:
                log.exception("capture_card_dtmf: mute(%s) failed", muted)
                return False

        async def _cleanup() -> None:
            """Restore caller audio + STT + LLM gating + remove DTMF handler.
            Always called; idempotent."""
            try:
                self._room.off("sip_dtmf_received", _on_dtmf)
            except Exception:
                log.exception("capture_card_dtmf cleanup: handler removal failed")
            await _set_mute(False)
            try:
                self.session.input.set_audio_enabled(True)
            except Exception:
                pass
            self._silent = False

        if not await _set_mute(True):
            try:
                self._room.off("sip_dtmf_received", _on_dtmf)
            except Exception:
                pass
            return json.dumps({
                "status": "error",
                "error": "Could not mute caller audio — refusing to start capture.",
            })

        # Belt + suspenders: gate STT input + short-circuit LLM turns during
        # the capture window. The mute should already make STT silent (no
        # audio frames published), but if anything slips through we don't
        # want the LLM responding.
        try:
            self.session.input.set_audio_enabled(False)
        except Exception:
            pass
        self._silent = True

        log.info(
            "capture_card_dtmf: muted caller=%s, beginning capture for res=%s",
            caller_participant.identity, reservation_id,
        )

        # ---- 4. Per-stage capture loop ----
        pan_local = ""
        cvc_local = ""
        try:
            for i, (stage_name, prompt_text) in enumerate(STAGES):
                stage_index[0] = i
                buffers[stage_name] = ""
                stage_complete.clear()
                stage_aborted.clear()

                log.info(
                    "capture_card_dtmf: entering stage %s (timeout=%ds)",
                    stage_name, STAGE_TIMEOUT_S,
                )

                # Say the stage prompt
                try:
                    await self.session.say(prompt_text, allow_interruptions=False)
                except Exception:
                    log.exception("capture_card_dtmf: say(%s prompt) failed", stage_name)

                # Wait for stage completion (`#`), abort (`*`), or timeout.
                # On timeout, re-prompt once before giving up.
                async def _wait_stage() -> str:
                    done_t = asyncio.create_task(stage_complete.wait())
                    abort_t = asyncio.create_task(stage_aborted.wait())
                    try:
                        done_set, pending = await asyncio.wait(
                            {done_t, abort_t},
                            timeout=STAGE_TIMEOUT_S,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    finally:
                        for t in (done_t, abort_t):
                            if not t.done():
                                t.cancel()
                    if not done_set:
                        return "timeout"
                    return "aborted" if abort_t in done_set else "complete"

                outcome = await _wait_stage()
                log.info(
                    "capture_card_dtmf: stage %s outcome=%s buffer_len=%d",
                    stage_name, outcome, len(buffers[stage_name]),
                )
                if outcome == "timeout":
                    try:
                        # Tailor the re-prompt based on whether any digits
                        # were collected — saying "I didn't get anything"
                        # when the caller pressed digits but forgot # is
                        # confusing.
                        if buffers[stage_name]:
                            await self.session.say(
                                "I got some digits but I'm still waiting for the pound sign. Press the pound key — that's the one with the hash marks — when you're done.",
                                allow_interruptions=False,
                            )
                        else:
                            await self.session.say(
                                "I didn't get anything. Let's try once more.",
                                allow_interruptions=False,
                            )
                            await self.session.say(prompt_text, allow_interruptions=False)
                    except Exception:
                        pass
                    stage_complete.clear()
                    stage_aborted.clear()
                    outcome = await _wait_stage()
                    log.info(
                        "capture_card_dtmf: stage %s second-attempt outcome=%s buffer_len=%d",
                        stage_name, outcome, len(buffers[stage_name]),
                    )
                    if outcome == "timeout":
                        log.warning("capture_card_dtmf: %s timeout twice", stage_name)
                        return json.dumps({"status": "timeout", "stage": stage_name})

                if outcome == "aborted":
                    log.info("capture_card_dtmf: caller aborted during %s", stage_name)
                    return json.dumps({"status": "aborted_by_user", "stage": stage_name})

            # ---- 5. Validate ----
            pan_local = buffers["pan"]
            exp_buf = buffers["exp"]
            cvc_local = buffers["cvc"]
            if not (13 <= len(pan_local) <= 19):
                log.warning("capture_card_dtmf: invalid PAN length=%d", len(pan_local))
                return json.dumps({
                    "status": "error",
                    "error": "Card number wasn't 13 to 19 digits.",
                    "stage": "pan",
                })
            if len(exp_buf) != 4:
                log.warning("capture_card_dtmf: invalid EXP length=%d", len(exp_buf))
                return json.dumps({
                    "status": "error",
                    "error": "Expiration wasn't four digits.",
                    "stage": "exp",
                })
            if not (3 <= len(cvc_local) <= 4):
                log.warning("capture_card_dtmf: invalid CVC length=%d", len(cvc_local))
                return json.dumps({
                    "status": "error",
                    "error": "Security code wasn't 3 or 4 digits.",
                    "stage": "cvc",
                })
            try:
                exp_month = int(exp_buf[:2])
                exp_year_yy = int(exp_buf[2:])
            except ValueError:
                return json.dumps({
                    "status": "error",
                    "error": "Expiration didn't parse as numbers.",
                    "stage": "exp",
                })
            if not (1 <= exp_month <= 12):
                return json.dumps({
                    "status": "error",
                    "error": "Expiration month wasn't 1 to 12.",
                    "stage": "exp",
                })
            exp_year = 2000 + exp_year_yy if exp_year_yy < 100 else exp_year_yy

            # ---- 6. Tokenize via Stripe ----
            try:
                await self.session.say("One moment.", allow_interruptions=False)
            except Exception:
                pass

            tok_result = await _stripe_tokenize(
                pan=pan_local,
                exp_month=exp_month,
                exp_year=exp_year,
                cvc=cvc_local,
                holder_name="",
            )

            # ---- 7. PCI scrub: zero out PAN/CVC in all references ----
            pan_local = ""
            cvc_local = ""
            buffers["pan"] = ""
            buffers["cvc"] = ""
            buffers["exp"] = ""

            if not tok_result.get("success"):
                code = tok_result.get("stripe_code", "")
                msg = tok_result.get("error", "Card processing error.")
                log.warning("capture_card_dtmf: Stripe declined: code=%s msg=%s", code, msg)
                # Stripe codes that represent a card the caller could
                # plausibly retry vs. our infra problems.
                soft_decline_codes = {
                    "card_declined", "incorrect_cvc", "expired_card",
                    "invalid_expiry_month", "invalid_expiry_year",
                    "invalid_number", "invalid_cvc", "processing_error",
                }
                if code in soft_decline_codes:
                    return json.dumps({"status": "declined", "reason": msg, "stripe_code": code})
                return json.dumps({"status": "error", "error": msg, "stripe_code": code})

            token_id = tok_result["token_id"]
            token_card = tok_result["token_card"]

            # ---- 8. Save to Cloudbeds via backend ----
            save = await _call_backend_tool(
                "save_card_via_token",
                {
                    "reservation_id": reservation_id,
                    "token_id": token_id,
                    "token_card": token_card,
                },
                self._caller_phone,
            )

            if not save.get("success"):
                log.warning("capture_card_dtmf: backend save failed: %s",
                            save.get("error"))
                return json.dumps({
                    "status": "error",
                    "error": save.get("error") or "Could not save card to reservation.",
                })

            log.info(
                "capture_card_dtmf: SUCCESS res=%s card_id=%s last4=%s brand=%s",
                reservation_id, save.get("card_id"), save.get("last4"),
                save.get("brand"),
            )
            return json.dumps({
                "status": "success",
                "card_id": save.get("card_id"),
                "last4": save.get("last4") or (token_card or {}).get("last4", ""),
                "brand": save.get("brand") or (token_card or {}).get("brand", ""),
            })

        except Exception:
            log.exception("capture_card_dtmf: unhandled exception")
            return json.dumps({"status": "error", "error": "Unexpected internal error."})
        finally:
            # Belt-and-suspenders scrub in case an exception path skipped step 7.
            pan_local = ""
            cvc_local = ""
            buffers["pan"] = ""
            buffers["cvc"] = ""
            buffers["exp"] = ""
            await _cleanup()


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
    cache = TTSAudioCache(max_entries=1000, persist_path=TTS_CACHE_PATH)
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
    # Pre-render persistent openers — but in a BACKGROUND THREAD so we don't
    # block prewarm. LiveKit Agents has a ~30s timeout on proc.initialize();
    # synthesizing 36 phrases at ~0.8-1.4s each blows that budget and the
    # framework kills the worker before it ever registers. Spawning a daemon
    # thread lets prewarm return immediately, the worker registers as ready,
    # and the openers render in parallel with whatever the agent is doing.
    # The background thread calls cache.save() at the end so the new entries
    # persist; subsequent prewarms find them already cached and skip them.
    # Build the prewarm worklist: PERSISTENT_OPENERS (greeting, transfer
    # flow, voice-admin confirmations) ∪ every SENTENCE inside every
    # response.text in the intent cache. Critical: prerender at the
    # sentence chunk level, NOT the full multi-sentence response level.
    # LiveKit's TTS pipeline splits multi-sentence responses on period/?/!
    # before each chunk is looked up in the audio cache. If we prerender
    # the full "It's $20 per stay. Cats aren't allowed, by the way." as
    # one entry, the speak path looks up TWO sub-chunks neither of which
    # match — both miss, both synthesize cold, and the caller hears a
    # 4-5s pause between the two sentences. Splitting at prewarm time
    # so the chunk keys line up with what the speak path will ask for.
    intent_response_chunks = INTENT_CACHE.all_response_chunks()
    seen_phrases: set[str] = set()
    prewarm_phrases: list[str] = []
    for phrase in list(PERSISTENT_OPENERS) + intent_response_chunks:
        if phrase not in seen_phrases:
            seen_phrases.add(phrase)
            prewarm_phrases.append(phrase)

    import threading
    import time
    # Stash an in-progress marker so the transcript can show "not yet done"
    # if a call completes before prewarm finishes. Replaced with the final
    # stats dict once the thread completes.
    proc.userdata["prewarm_stats"] = {
        "status": "running",
        "total_phrases": len(prewarm_phrases),
        "started_at": datetime.now().isoformat(),
    }

    def _bg_prerender_phrases() -> None:
        start = time.monotonic()
        rendered = skipped = failed = 0
        synth_seconds = 0.0
        total = len(prewarm_phrases)
        last_progress_log = start
        for i, phrase in enumerate(prewarm_phrases, 1):
            if tts.cache_key(phrase) in cache:
                skipped += 1
                continue
            try:
                t0 = time.monotonic()
                tts.prerender(phrase)
                synth_seconds += time.monotonic() - t0
                rendered += 1
            except Exception:
                failed += 1
                log.exception("Failed to prerender phrase %r", phrase)
            # Progress log every 15s of wall time so journalctl shows how
            # far prewarm has gotten without waiting for the final summary.
            now = time.monotonic()
            if now - last_progress_log > 15.0:
                pct = round(100 * i / total) if total else 0
                log.info(
                    "Prewarm progress: %d/%d (%d%%) — rendered=%d skipped=%d failed=%d, %.1fs elapsed",
                    i, total, pct, rendered, skipped, failed, now - start,
                )
                last_progress_log = now
        elapsed = time.monotonic() - start
        avg_synth = (synth_seconds / rendered) if rendered else 0.0
        log.info(
            "Prewarm DONE in %.1fs (synth time %.1fs, avg %.2fs/phrase): "
            "rendered=%d skipped=%d failed=%d "
            "(openers=%d, intent_cache_chunks=%d, total=%d)",
            elapsed, synth_seconds, avg_synth,
            rendered, skipped, failed,
            len(PERSISTENT_OPENERS), len(intent_response_chunks),
            len(prewarm_phrases),
        )
        save_seconds = 0.0
        if rendered:
            t0 = time.monotonic()
            cache.save()
            save_seconds = time.monotonic() - t0
            log.info(
                "Persisted %d new prewarm entries to disk in %.2fs",
                rendered, save_seconds,
            )
        # Replace the running marker with the completion stats. Read by
        # write_transcript() to embed in the per-call JSON.
        proc.userdata["prewarm_stats"] = {
            "status": "done",
            "elapsed_seconds": round(elapsed, 2),
            "synth_seconds": round(synth_seconds, 2),
            "save_seconds": round(save_seconds, 2),
            "avg_synth_seconds": round(avg_synth, 3),
            "rendered": rendered,
            "skipped": skipped,
            "failed": failed,
            "total": len(prewarm_phrases),
            "openers": len(PERSISTENT_OPENERS),
            "intent_cache_chunks": len(intent_response_chunks),
            "finished_at": datetime.now().isoformat(),
        }
    log.info(
        "Spawning background prerender of %d phrases (%d openers + %d intent-cache sentence chunks, deduped to %d, non-blocking)...",
        len(prewarm_phrases),
        len(PERSISTENT_OPENERS), len(intent_response_chunks),
        len(prewarm_phrases),
    )
    threading.Thread(
        target=_bg_prerender_phrases, daemon=True, name="prerender-phrases",
    ).start()
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

    # Diagnostic instrumentation 2026-05-15: log every participant
    # join/leave on this room so we can correlate Python-side state
    # with LiveKit-SIP bridge state. For silent-mode 5070 transfers
    # we expect to see (1) the inbound SIP caller joining first, then
    # (2) the outbound SIP participant (frontdesk2) joining once the
    # transfer completes. If we see (2) join but audio doesn't flow,
    # the bug is in livekit-sip's RTP relay, not in our agent code.
    @ctx.room.on("participant_connected")
    def _on_pc(participant) -> None:
        log.info(
            "ROOM: participant_connected identity=%s kind=%s sid=%s attrs=%s",
            getattr(participant, "identity", "<unknown>"),
            getattr(participant, "kind", "<unknown>"),
            getattr(participant, "sid", "<unknown>"),
            dict(getattr(participant, "attributes", {}) or {}),
        )

    @ctx.room.on("participant_disconnected")
    def _on_pd(participant) -> None:
        log.info(
            "ROOM: participant_disconnected identity=%s kind=%s sid=%s reason=%s",
            getattr(participant, "identity", "<unknown>"),
            getattr(participant, "kind", "<unknown>"),
            getattr(participant, "sid", "<unknown>"),
            getattr(participant, "disconnect_reason", "<unknown>"),
        )

    @ctx.room.on("track_subscribed")
    def _on_ts(track, publication, participant) -> None:
        log.info(
            "ROOM: track_subscribed from=%s kind=%s source=%s muted=%s",
            getattr(participant, "identity", "<unknown>"),
            getattr(track, "kind", "<unknown>"),
            getattr(publication, "source", "<unknown>"),
            getattr(publication, "muted", "<unknown>"),
        )

    @ctx.room.on("track_unsubscribed")
    def _on_tu(track, publication, participant) -> None:
        log.info(
            "ROOM: track_unsubscribed from=%s kind=%s source=%s",
            getattr(participant, "identity", "<unknown>"),
            getattr(track, "kind", "<unknown>"),
            getattr(publication, "source", "<unknown>"),
        )

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
    # We also pull the DID the caller dialed (`sip.trunkPhoneNumber` is
    # the most reliable name across LiveKit-SIP versions) so the agent
    # can branch behavior per-DID (see IMMEDIATE_TRANSFER_DID).
    participant = await ctx.wait_for_participant()
    caller_phone: str | None = None
    called_number: str | None = None
    if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
        attrs = participant.attributes
        caller_phone = attrs.get("sip.phoneNumber")
        # Try a few attribute names; LiveKit-SIP versions have varied.
        called_number = (
            attrs.get("sip.trunkPhoneNumber")
            or attrs.get("sip.toNumber")
            or attrs.get("sip.callee")
        )
        # Log all attributes once so we can verify the called-number
        # attribute name and learn what else LiveKit exposes.
        log.info("SIP participant attributes: %s", dict(attrs))
    log.info("Caller participant=%s phone=%s called=%s kind=%s",
             participant.identity, caller_phone, called_number, participant.kind)

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
    # TTFT, duration, token counts.
    #
    # Prompt-caching field: in livekit-agents 1.5.8, the Anthropic plugin
    # exposes the cache-read count as `prompt_cached_tokens` directly on
    # the LLMMetrics object (not as `cache_read_input_tokens` on a nested
    # `.usage` object as the raw Anthropic SDK does). There's no
    # cache_creation field surfaced — we infer it as
    # `prompt_tokens - prompt_cached_tokens` (= the portion the server
    # actually had to process). For a healthy cache, prompt_cached_tokens
    # should grow large on turn 2+ and stay roughly steady (~the size of
    # the cached system prompt + tools) for the rest of the call.
    @session.on("metrics_collected")
    def _on_metrics(ev) -> None:
        m = getattr(ev, "metrics", None)
        if m is None:
            return
        kind = type(m).__name__
        attrs = {}
        for k in ("ttft", "duration", "prompt_tokens", "completion_tokens",
                  "prompt_cached_tokens", "total_tokens",
                  "input_tokens", "output_tokens", "audio_duration",
                  "characters_count", "tokens_per_second"):
            v = getattr(m, k, None)
            if v is not None:
                attrs[k] = round(v, 3) if isinstance(v, float) else v
        # Derived cache hit ratio for the LLM call. Helpful for spotting
        # cache misses without doing math in the head.
        if "prompt_cached_tokens" in attrs and "prompt_tokens" in attrs and attrs["prompt_tokens"]:
            attrs["cache_hit_ratio"] = round(
                attrs["prompt_cached_tokens"] / attrs["prompt_tokens"], 3
            )
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

            # Background prewarm thread's timing. If status == "running",
            # this call started before prewarm finished, which means some
            # TTS lookups during the call may have synthesized cold even
            # though the phrase is in our prewarm list. Once status ==
            # "done", subsequent calls find everything in the disk pickle.
            prewarm_stats = ctx.proc.userdata.get("prewarm_stats")

            transcript = {
                "room": ctx.room.name,
                "caller_phone": caller_phone,
                "started_at": started_at.isoformat(),
                "ended_at": datetime.now().isoformat(),
                "duration_seconds": (datetime.now() - started_at).total_seconds(),
                "item_count": len(items),
                "event_count": len(events),
                "tts_cache_stats": cache_stats,
                "prewarm_stats": prewarm_stats,
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
            called_number=called_number,
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
