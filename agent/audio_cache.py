"""In-memory LRU cache of synthesized TTS audio (raw PCM bytes).

Why this exists:
  - Pre-rendered phrases (greeting) play instantly without invoking Kokoro.
  - LLM-produced phrases that repeat across turns / calls are served from
    cache instead of resynthesizing.

Why disk persistence matters:
  LiveKit Agents spawns a fresh worker subprocess for each new job (with
  num_idle_processes=1). The cache lives in worker memory, so it dies
  with the worker. Without disk persistence we'd never get cross-call
  cache hits — each call would start with a "fresh" cache containing
  only what got pre-rendered at worker startup.

  Persisting on shutdown + loading on startup lets the cache grow
  organically across calls. The greeting alone is enough to feel snappy
  on call open; cross-call hits make repeated answers also fast.

Design:
  Single LRU cache (`OrderedDict`), evicts the least-recently-used entry
  past `max_entries`. Persists as a pickled dict to `persist_path` if
  provided. Atomic file replace on save so a half-written cache won't
  corrupt the file on crash.
"""
from __future__ import annotations

import hashlib
import logging
import pickle
import re
import wave
from collections import OrderedDict
from pathlib import Path
from threading import Lock

log = logging.getLogger("audio_cache")

# Bump this when changes to kokoro_tts.py's text normalization (or any
# transformation that affects the audio bytes) make previously-cached
# entries stale. On load, the cache compares its pickled version to this
# constant; on mismatch, all entries are dropped and the cache rebuilds
# fresh. Avoids hand-wiping `tts_cache.pkl` after every fix.
#
# History:
#   1 - initial schema
#   2 - kokoro_tts._normalize_for_tts: $NN -> "N dollars", strip hyphen
#       from X-dollar(s). Caused by Iris pronouncing "$20" as "Dollar
#       twenty" / "twenty-dollar" as "dollar twenty".
CACHE_VERSION = 2


class TTSAudioCache:
    def __init__(
        self,
        *,
        max_entries: int = 500,
        persist_path: Path | None = None,
    ) -> None:
        # OrderedDict gives O(1) move_to_end (mark-as-MRU) and O(1)
        # popitem(last=False) (evict LRU). Iteration order is LRU first,
        # MRU last — useful for debugging which entries are next to go.
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._max = max_entries
        self._hits = 0
        self._misses = 0
        self._lock = Lock()
        self._persist_path = persist_path
        self._load()

    # ----- persistence -----

    def _load(self) -> None:
        """Load a previously-pickled cache from `persist_path`, if any.

        Tolerates corrupt / missing files — just starts fresh. Also
        drops the whole cache if the pickled `version` doesn't match
        the current `CACHE_VERSION` (handles the "I changed the TTS
        normalization and old entries have stale audio" case without
        a manual file wipe).
        """
        if self._persist_path is None or not self._persist_path.exists():
            return
        try:
            with open(self._persist_path, "rb") as f:
                data = pickle.load(f)
            loaded_version = data.get("version")
            if loaded_version != CACHE_VERSION:
                log.info(
                    "TTS cache schema mismatch (loaded=%s, current=%s); "
                    "dropping all entries — they'll resynth with current code",
                    loaded_version, CACHE_VERSION,
                )
                return
            entries = data.get("entries")
            if isinstance(entries, OrderedDict):
                self._cache = entries
                log.info(
                    "Loaded %d TTS cache entries from %s",
                    len(self._cache), self._persist_path,
                )
        except Exception as e:
            log.warning(
                "TTS cache load failed (%s); starting fresh", e,
            )

    def save(self) -> None:
        """Pickle current cache to `persist_path`. Atomic rename pattern
        so a crash mid-write can't leave a corrupt file in place.

        Safe to call from a synchronous context (no asyncio dependencies).
        """
        if self._persist_path is None:
            return
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._persist_path.with_suffix(".tmp")
        try:
            with self._lock:
                payload = {
                    "version": CACHE_VERSION,
                    "entries": OrderedDict(self._cache),
                }
            with open(tmp, "wb") as f:
                pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
            tmp.replace(self._persist_path)
            log.info(
                "Saved %d TTS cache entries (v%s) to %s",
                len(payload["entries"]), CACHE_VERSION, self._persist_path,
            )
        except Exception as e:
            log.warning("TTS cache save failed: %s", e)
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    # ----- read/write -----

    def get(self, text: str) -> bytes | None:
        with self._lock:
            if text in self._cache:
                # Touch: mark this entry as MRU so eviction skips it.
                self._cache.move_to_end(text)
                self._hits += 1
                return self._cache[text]
            self._misses += 1
            return None

    def put(self, text: str, audio: bytes) -> None:
        """Insert or refresh an entry. New entry becomes MRU; if cache
        is full, the LRU entry is evicted."""
        with self._lock:
            if text in self._cache:
                self._cache.move_to_end(text)
                return
            if len(self._cache) >= self._max:
                self._cache.popitem(last=False)
            self._cache[text] = audio

    # ----- diagnostics -----

    def __contains__(self, text: str) -> bool:
        with self._lock:
            return text in self._cache

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "entries": len(self._cache),
                "max": self._max,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 3) if total else 0.0,
            }

    # ----- WAV export (for human inspection / download) -----

    def dump_to_wav_dir(
        self,
        dir_path: Path,
        sample_rate: int = 24000,
        wipe_first: bool = True,
    ) -> int:
        """Write each cache entry as a 16-bit mono WAV in `dir_path`.

        Filenames are sanitized phrase prefix + short MD5 hash, e.g.
        ``lighthouse_inn_this_is_iris_the_ai_assistant_9c1a3d2f.wav``.
        Hash suffix prevents collisions when two phrases sanitize to the
        same prefix.

        If `wipe_first` is True (default), existing .wav files in
        `dir_path` are removed before writing. This keeps the dir in
        sync with the current cache — entries that got evicted (LRU) or
        dropped (version bump) don't leave stale WAVs behind.

        Returns number of files written.
        """
        dir_path.mkdir(parents=True, exist_ok=True)
        if wipe_first:
            for f in dir_path.glob("*.wav"):
                try:
                    f.unlink()
                except OSError as e:
                    log.warning("Could not remove %s: %s", f, e)
        count = 0
        with self._lock:
            snapshot = list(self._cache.items())
        for text, pcm in snapshot:
            safe = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()[:60]
            h = hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
            wav_path = dir_path / f"{safe}_{h}.wav"
            try:
                with wave.open(str(wav_path), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(sample_rate)
                    w.writeframes(pcm)
                count += 1
            except OSError as e:
                log.warning("Could not write %s: %s", wav_path, e)
        return count
