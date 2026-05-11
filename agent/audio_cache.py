"""In-memory cache of synthesized TTS audio (raw PCM bytes).

Two purposes:
  1. Pre-render fixed phrases (greeting, common closings) at worker
     startup so the first call has zero TTS latency for those.
  2. Auto-cache LLM-produced phrases as they're synthesized, so repeated
     identical sentences across calls bypass Kokoro after the first hit.

Cache keys are the literal text TTS.synthesize() receives. The framework
splits LLM output at sentence boundaries before calling synthesize, so
cache effectively works at sentence granularity.

Fixed entries are permanent; auto entries use LRU eviction (least-
recently-used dropped first) past `max_auto_entries`. Every read and
write on the auto cache moves the entry to the most-recently-used end.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from threading import Lock

log = logging.getLogger("audio_cache")


class TTSAudioCache:
    def __init__(self, max_auto_entries: int = 200) -> None:
        self._fixed: dict[str, bytes] = {}
        # OrderedDict gives us O(1) move_to_end (mark-as-recently-used) and
        # O(1) popitem(last=False) (evict-LRU). Iteration order = LRU first,
        # MRU last — useful for inspecting which entries are about to be
        # dropped if you want to debug.
        self._auto: OrderedDict[str, bytes] = OrderedDict()
        self._max = max_auto_entries
        self._hits = 0
        self._misses = 0
        self._lock = Lock()

    def get(self, text: str) -> bytes | None:
        with self._lock:
            if text in self._fixed:
                self._hits += 1
                return self._fixed[text]
            if text in self._auto:
                # Touch: move to MRU end so it survives eviction longer.
                self._auto.move_to_end(text)
                self._hits += 1
                return self._auto[text]
            self._misses += 1
            return None

    def put_fixed(self, text: str, audio: bytes) -> None:
        with self._lock:
            self._fixed[text] = audio

    def put_auto(self, text: str, audio: bytes) -> None:
        with self._lock:
            if text in self._fixed:
                return
            if text in self._auto:
                # Already auto-cached — refresh its position.
                self._auto.move_to_end(text)
                return
            if len(self._auto) >= self._max:
                # Drop the least-recently-used entry.
                self._auto.popitem(last=False)
            self._auto[text] = audio

    def __contains__(self, text: str) -> bool:
        with self._lock:
            return text in self._fixed or text in self._auto

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "fixed": len(self._fixed),
                "auto": len(self._auto),
                "auto_max": self._max,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 3) if total else 0.0,
            }
