"""In-memory cache of synthesized TTS audio (raw PCM bytes).

Two purposes:
  1. Pre-render fixed phrases (greeting, common closings) at worker
     startup so the first call has zero TTS latency for those.
  2. Auto-cache LLM-produced phrases as they're synthesized, so repeated
     identical sentences across calls bypass Kokoro after the first hit.

Cache keys are the literal text TTS.synthesize() receives. The framework
splits LLM output at sentence boundaries before calling synthesize, so
cache effectively works at sentence granularity.

Fixed entries are permanent; auto entries are FIFO-evicted past max.
"""
from __future__ import annotations

import logging
from threading import Lock

log = logging.getLogger("audio_cache")


class TTSAudioCache:
    def __init__(self, max_auto_entries: int = 200) -> None:
        self._fixed: dict[str, bytes] = {}
        self._auto: dict[str, bytes] = {}
        self._auto_order: list[str] = []
        self._max = max_auto_entries
        self._lock = Lock()

    def get(self, text: str) -> bytes | None:
        with self._lock:
            if text in self._fixed:
                return self._fixed[text]
            return self._auto.get(text)

    def put_fixed(self, text: str, audio: bytes) -> None:
        with self._lock:
            self._fixed[text] = audio

    def put_auto(self, text: str, audio: bytes) -> None:
        with self._lock:
            if text in self._fixed or text in self._auto:
                return
            if len(self._auto) >= self._max:
                oldest = self._auto_order.pop(0)
                self._auto.pop(oldest, None)
            self._auto[text] = audio
            self._auto_order.append(text)

    def __contains__(self, text: str) -> bool:
        with self._lock:
            return text in self._fixed or text in self._auto

    def stats(self) -> dict:
        with self._lock:
            return {
                "fixed": len(self._fixed),
                "auto": len(self._auto),
                "auto_max": self._max,
            }
