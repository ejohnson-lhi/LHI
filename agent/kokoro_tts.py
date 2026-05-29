"""Self-hosted Kokoro TTS adapter for LiveKit Agents 1.5.x.

Wraps the kokoro-onnx package (https://github.com/thewh1teagle/kokoro-onnx)
as a livekit.agents.tts.TTS subclass. Synthesis runs in a thread pool so it
doesn't block the asyncio event loop (ONNX Runtime releases the GIL during
inference).

Usage in the agent:
    from kokoro_tts import KokoroTTS

    session = AgentSession(
        ...
        tts=KokoroTTS(
            model_path="models/kokoro-v1.0.onnx",
            voices_path="models/voices-v1.0.bin",
            voice="af_sarah",
        ),
    )

System dependency: `sudo apt install espeak-ng` (used by kokoro-onnx for
phonemization). Without it, the first synthesize() call fails with a
confusing error.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import numpy as np
from kokoro_onnx import Kokoro
from livekit.agents import (
    APIConnectionError,
    APIConnectOptions,
    tts,
    utils,
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

from audio_cache import TTSAudioCache

log = logging.getLogger(__name__)

# Kokoro v1.0 always returns 24 kHz mono float32 in [-1, 1].
KOKORO_SAMPLE_RATE = 24000
KOKORO_NUM_CHANNELS = 1


# espeak-ng (the phonemizer kokoro-onnx wraps) puts "Dollar" BEFORE the
# number in two patterns:
#   1. Hyphenated compound: "twenty-dollar fee" -> "dollar twenty fee"
#   2. Dollar-sign prefix:  "$20 fee"           -> "dollar twenty fee"
# Both sound like "$1.20" or similar. Rewrite to "N dollars/dollar" so
# espeak reads the natural order.
_HYPHEN_DOLLAR_RE = re.compile(r"\b(\w+)-(dollars?)\b", re.IGNORECASE)
_DOLLAR_PREFIX_RE = re.compile(r"\$(\d+)\b")


def _normalize_for_tts(text: str) -> str:
    text = _HYPHEN_DOLLAR_RE.sub(r"\1 \2", text)
    # Singular "dollar" (not "dollars"): the LLM's usual phrasing puts the
    # number in adjectival position ("a $20 fee" → "a 20 dollar fee").
    # Plural would make it "a 20 dollars fee" which sounds awkward.
    text = _DOLLAR_PREFIX_RE.sub(r"\1 dollar", text)
    return text


# Split a multi-sentence utterance into clauses for per-clause cache lookup +
# pipelined synthesis. The pattern matches end-of-sentence punctuation
# followed by whitespace and a capital letter -- the classic English
# sentence boundary signal. Lookbehind / lookahead keep the punctuation
# attached to the LEFT side ("Hello. World." -> ["Hello.", "World."]).
#
# Edge cases worth knowing about:
#   - "Mr. Smith" wouldn't be split because there's no capital-after-period
#     boundary inside "Mr. " followed by "Smith" (well, "S" IS capital --
#     so this WOULD wrongly split). The hotel domain rarely produces "Mr."
#     in Iris's output (she uses guest first names + last names without
#     titles), so this is acceptable risk. Add an exclusion list if it
#     bites us in practice.
#   - Decimal numbers like "$5.99" don't match (no space after the period).
#   - Quoted sentences end on the closing quote, not the period inside --
#     we ignore this complexity since Iris doesn't typically quote.
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def _split_into_clauses(text: str) -> list[str]:
    """Split an Iris utterance into sentence-level clauses for per-clause
    cache lookup + pipelined synthesis.

    Returns a list of trimmed clauses. Empty input returns []. A
    single-sentence input returns a single-element list (the whole
    text). Clauses are stripped of leading/trailing whitespace so cache
    keys match prewarmed entries (the prewarm list stores phrases
    without trailing whitespace).

    Two-sentence utterances like "Of course. Let me check that for you."
    return ["Of course.", "Let me check that for you."]. With both
    entries in the PERSISTENT_OPENERS prewarm list, both halves hit the
    cache and Iris speaks instantly. A confirmation like
    "Just to confirm... June 3. Is that correct?" returns
    ["Just to confirm... June 3.", "Is that correct?"] -- the long
    dynamic half still renders fresh, but "Is that correct?" hits the
    cache, eliminating the trailing render delay.

    We deliberately DON'T coalesce short clauses. Short standalone
    phrases ("Sure.", "Of course.", "Yes.") are exactly the high-value
    cache-hit candidates -- merging them into a longer neighbor would
    create a fresh-render key that misses the cache. The synthesis
    overhead per clause is small (a few ms of Python + a single ONNX
    inference whose cost scales with audio length, not clause count).
    """
    text = text.strip()
    if not text:
        return []
    parts = _SENTENCE_BOUNDARY_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


@dataclass
class _Opts:
    voice: str
    speed: float
    lang: str


class KokoroTTS(tts.TTS):
    """Self-hosted Kokoro v1.0 TTS as a LiveKit Agents TTS provider."""

    def __init__(
        self,
        *,
        model_path: str,
        voices_path: str,
        voice: str = "af_sarah",
        speed: float = 1.0,
        lang: str = "en-us",
        cache: TTSAudioCache | None = None,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=KOKORO_SAMPLE_RATE,
            num_channels=KOKORO_NUM_CHANNELS,
        )
        self._opts = _Opts(voice=voice, speed=speed, lang=lang)
        # Load the 325 MB ONNX model + 36 MB voices file once at startup.
        # Takes 3-8 seconds. Reused across every synthesize() call.
        log.info("Loading Kokoro model from %s", model_path)
        self._kokoro = Kokoro(model_path=model_path, voices_path=voices_path)
        available = set(self._kokoro.get_voices())
        if voice not in available:
            raise ValueError(
                f"Kokoro voice {voice!r} not available. "
                f"Choose from: {sorted(v for v in available if v.startswith(('af_', 'am_')))}"
            )
        log.info("Kokoro loaded; %d total voices, using %s", len(available), voice)
        # Audio cache lets us serve pre-rendered greeting + repeated LLM
        # phrases without invoking Kokoro at all. See agent/audio_cache.py.
        self._cache = cache if cache is not None else TTSAudioCache()

    def cache_stats(self) -> dict:
        """Snapshot of the audio cache: entry counts + lifetime hit rate."""
        return self._cache.stats()

    def cache_key(self, text: str) -> str:
        """Cache key for `text` under the current voice.

        Different voices produce different audio for the same input text,
        so the voice has to be part of the key — otherwise switching
        IRIS_VOICE would serve stale audio from the wrong voice. The key
        is also engine-prefixed (`[kokoro:voice]text`) so a future
        ElevenLabs migration with an overlapping voice name can't return
        Kokoro audio. CACHE_VERSION bumps on schema change drop old keys.
        """
        return f"[kokoro:{self._opts.voice}]{text}"

    def prerender(self, text: str) -> None:
        """Synthesize ``text`` synchronously and stash it in the cache.

        Call this at worker startup for greeting and similar always-used
        phrases so subsequent synthesize() calls return instantly. Touch
        on lookup keeps these at MRU end of the LRU, so they don't get
        evicted as long as they're used at the start of every call.

        Skips if already cached (idempotent — handy when loading a
        persisted cache then re-prewarming).
        """
        key = self.cache_key(text)
        if key in self._cache:
            return
        samples, sr = self._kokoro.create(
            _normalize_for_tts(text),
            voice=self._opts.voice,
            speed=self._opts.speed,
            lang=self._opts.lang,
        )
        if sr != KOKORO_SAMPLE_RATE:
            raise APIConnectionError(
                f"Unexpected Kokoro sample rate {sr}, expected {KOKORO_SAMPLE_RATE}"
            )
        pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
        self._cache.put(key, pcm)
        log.info(
            "Pre-rendered (%d bytes, %.2fs, voice=%s): %r",
            len(pcm), len(samples) / sr, self._opts.voice, text[:80],
        )

    @property
    def model(self) -> str:
        return "kokoro-v1.0"

    @property
    def provider(self) -> str:
        return "kokoro-onnx"

    def update_options(
        self,
        *,
        voice: str | None = None,
        speed: float | None = None,
        lang: str | None = None,
    ) -> None:
        if voice is not None:
            self._opts.voice = voice
        if speed is not None:
            self._opts.speed = speed
        if lang is not None:
            self._opts.lang = lang

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> "_KokoroChunkedStream":
        return _KokoroChunkedStream(
            tts=self,
            input_text=text,
            conn_options=conn_options,
        )


class _KokoroChunkedStream(tts.ChunkedStream):
    def __init__(
        self,
        *,
        tts: KokoroTTS,
        input_text: str,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts: KokoroTTS = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        opts = self._tts._opts
        request_id = utils.shortuuid()

        # mime_type "audio/pcm" tells AudioEmitter to skip its decoder and
        # feed bytes straight into an AudioByteStream that hands frames to
        # the room at the framework's default chunk size (~200 ms frames).
        output_emitter.initialize(
            request_id=request_id,
            sample_rate=KOKORO_SAMPLE_RATE,
            num_channels=KOKORO_NUM_CHANNELS,
            mime_type="audio/pcm",
        )

        text = self.input_text

        # WARMUP SHORT-CIRCUIT: if the LLM returned just "Hello" (the
        # canonical response to the internal "blizzard frog" warmup signal
        # -- see iris_agent.py _WARMUP_SENTINEL and the [Internal Warmup
        # Signal] rule in AI_Prompts/Lighthouse_AI_system_prompt.txt),
        # emit ~50 ms of silence instead of synthesizing. The warmup turn
        # exists only to populate Anthropic's prompt cache with a prefix
        # that matches the real first turn byte-for-byte; the caller
        # should not hear it.
        #
        # Safety analysis: bare "Hello" is virtually never the LLM's
        # standalone response during a real call. Real greetings always
        # include "Lighthouse Inn" / persona name (see PERSISTENT_OPENERS
        # in iris_agent.py), and mid-call openers are phrases like
        # "Of course." or "Sure." -- not "Hello". If the LLM ever does
        # respond with bare "Hello" in a real call, the caller hears a
        # 50 ms blip instead of a syllable; recoverable, not catastrophic.
        # Matching strips trailing punctuation/whitespace and lowercases.
        normalized = text.strip().rstrip(".!?,").strip().lower()
        if normalized == "hello":
            silent_ms = 50
            silent_samples = int(KOKORO_SAMPLE_RATE * silent_ms / 1000)
            silent_pcm = np.zeros(silent_samples, dtype=np.int16).tobytes()
            log.info(
                "TTS warmup short-circuit: emitting %dms of silence for 'Hello' (text=%r)",
                silent_ms, text,
            )
            output_emitter.push(silent_pcm)
            output_emitter.flush()
            return

        kokoro = self._tts._kokoro

        # FAST PATH: full-utterance cache hit. Covers prewarmed greetings
        # and any utterance that's been spoken verbatim before. Preserves
        # the old single-key behavior so existing cache entries (and the
        # prewarm pass) keep their value.
        full_key = self._tts.cache_key(text)
        cached_full = self._tts._cache.get(full_key)
        if cached_full is not None:
            log.info("TTS cache hit (full, %d bytes): %r", len(cached_full), text[:80])
            output_emitter.push(cached_full)
            output_emitter.flush()
            return

        # SLOW PATH: split into clauses, render each independently, push
        # as ready. Two benefits:
        #
        #   1. Per-clause cache hits within an otherwise-fresh utterance.
        #      A response like "Of course. Let me check that for you."
        #      where the first half is in PERSISTENT_OPENERS but the
        #      full string isn't, now hits the cache on clause 1 (~60ms)
        #      and only renders clause 2 fresh. Previously this rendered
        #      both halves end-to-end (~2.5s).
        #
        #   2. Audio starts playing as soon as the first clause is ready.
        #      Subsequent clauses render during playback. For a 4-clause
        #      6-second utterance, perceived start-of-speech can drop
        #      from ~6s (full render) to ~1.5s (first clause render).
        #
        # If splitting yields just one clause (Iris said a single
        # sentence), this devolves to the same path as before but with
        # the per-clause cache key, which is identical to the full-key
        # we already missed -- one extra dict lookup, no extra work.
        clauses = _split_into_clauses(text)
        if not clauses:
            output_emitter.flush()
            return

        def _synth_clause(clause_text: str) -> bytes:
            # Run synthesis AND the float32→int16 conversion on the worker
            # thread. The numpy clip/multiply/cast looks cheap but on a
            # CPU-constrained host with ONNX Runtime intra-op threads still
            # winding down, this can hold the asyncio loop's GIL for tens of
            # ms -- long enough to underrun ParticipantAudioOutput's
            # ~250 ms buffer and produce within-word silence gaps in the
            # published Opus stream. Keeping everything off-loop fixes it.
            samples, sr = kokoro.create(
                _normalize_for_tts(clause_text),
                voice=opts.voice, speed=opts.speed, lang=opts.lang,
            )
            if sr != KOKORO_SAMPLE_RATE:
                raise APIConnectionError(
                    f"Unexpected Kokoro sample rate {sr}, expected {KOKORO_SAMPLE_RATE}"
                )
            pcm16 = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
            return pcm16.tobytes()

        hits = 0
        misses = 0
        for clause in clauses:
            key = self._tts.cache_key(clause)
            cached = self._tts._cache.get(key)
            if cached is not None:
                output_emitter.push(cached)
                hits += 1
                continue
            try:
                pcm_bytes = await asyncio.to_thread(_synth_clause, clause)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Anything from kokoro-onnx (bad text, ORT crash) surfaces
                # here. Wrap so the framework's retry/backoff sees a known
                # exception. Partial audio already pushed for prior clauses
                # is acceptable -- the framework will discard it on retry.
                raise APIConnectionError(f"Kokoro synthesis failed: {e}") from e
            # Cache the per-clause result. Two clauses with the same text
            # under the same voice share one entry. Across calls, repeated
            # boilerplate ("Is that correct?", "Got it.", "Of course.")
            # builds up cache hit rate naturally.
            self._tts._cache.put(key, pcm_bytes)
            output_emitter.push(pcm_bytes)
            misses += 1

        # Cache the full utterance too so a second occurrence of this EXACT
        # text gets the fast path on the next call (avoids the per-clause
        # iteration cost). Concatenate the bytes from each clause -- byte
        # ordering matches what we just emitted to the room. Skip the
        # full-cache write if it'd duplicate a single clause (the per-clause
        # cache already has it).
        if len(clauses) > 1:
            try:
                full_pcm = b"".join(
                    self._tts._cache.get(self._tts.cache_key(c)) or b""
                    for c in clauses
                )
                if full_pcm:
                    self._tts._cache.put(full_key, full_pcm)
            except Exception:  # noqa: BLE001
                # Cache write is best-effort. If join fails for any reason,
                # the per-clause cache still works for next time.
                log.exception("Failed to write full-utterance cache (clauses cached OK)")

        log.info(
            "TTS clauses: %d hit, %d miss, text=%r",
            hits, misses, text[:80],
        )
        output_emitter.flush()
