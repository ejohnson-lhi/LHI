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

    def prerender(self, text: str) -> None:
        """Synthesize ``text`` synchronously and stash it in the permanent
        cache. Call this at worker startup for fixed phrases (greeting,
        common closings) so subsequent synthesize() calls with the same
        text return instantly.

        Skips if already cached (idempotent).
        """
        if text in self._cache:
            return
        samples, sr = self._kokoro.create(
            text,
            voice=self._opts.voice,
            speed=self._opts.speed,
            lang=self._opts.lang,
        )
        if sr != KOKORO_SAMPLE_RATE:
            raise APIConnectionError(
                f"Unexpected Kokoro sample rate {sr}, expected {KOKORO_SAMPLE_RATE}"
            )
        pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
        self._cache.put_fixed(text, pcm)
        log.info(
            "Pre-rendered (%d bytes, %.2fs): %r",
            len(pcm), len(samples) / sr, text[:80],
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

        # Cache hit: skip synth entirely, push the pre-rendered bytes.
        cached = self._tts._cache.get(text)
        if cached is not None:
            log.info("TTS cache hit (%d bytes): %r", len(cached), text[:80])
            output_emitter.push(cached)
            output_emitter.flush()
            return

        kokoro = self._tts._kokoro

        def _synth_and_encode() -> bytes:
            # Run synthesis AND the float32→int16 conversion on the worker
            # thread. The numpy clip/multiply/cast looks cheap but on a
            # CPU-constrained host with ONNX Runtime intra-op threads still
            # winding down, this can hold the asyncio loop's GIL for tens of
            # ms. That long enough to underrun ParticipantAudioOutput's
            # ~250 ms buffer and produce within-word silence gaps in the
            # published Opus stream. Keeping everything off-loop fixes it.
            samples, sr = kokoro.create(text, voice=opts.voice, speed=opts.speed, lang=opts.lang)
            if sr != KOKORO_SAMPLE_RATE:
                raise APIConnectionError(
                    f"Unexpected Kokoro sample rate {sr}, expected {KOKORO_SAMPLE_RATE}"
                )
            # samples is float32 in [-1, 1]. LiveKit wants 16-bit signed PCM
            # little-endian. Clip first to avoid overflow on out-of-range
            # samples (rare but defensive).
            pcm16 = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
            return pcm16.tobytes()

        try:
            pcm_bytes = await asyncio.to_thread(_synth_and_encode)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Anything from kokoro-onnx (bad text, ORT crash) surfaces here.
            # Wrap so the framework's retry/backoff sees a known exception.
            raise APIConnectionError(f"Kokoro synthesis failed: {e}") from e

        # Auto-cache: next time the framework asks for this exact sentence,
        # we'll serve it from cache. Useful for frequently-repeated phrases
        # ("Is there anything else I can help you with today?", greeting
        # variants, common direct-answer phrasings, etc.).
        self._tts._cache.put_auto(text, pcm_bytes)

        output_emitter.push(pcm_bytes)
        output_emitter.flush()
