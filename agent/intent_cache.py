"""Intent cache: STT-side deflection of static-fact Q&A.

Sits between STT and the LLM. When the caller asks something whose answer
doesn't depend on the date or who is calling (pet fee, check-in time, WiFi
availability, etc.), match it to a pre-curated intent and pick one of
several pre-rendered response variants. Speak it via the TTS audio cache,
skip the LLM entirely. ~300ms response vs ~7s for an LLM-driven turn.

Data lives in `intent_cache.json` next to this file. See that file's `_doc`
for schema. Hot-loaded once at module import; reload by restarting the
worker (or editing live with `IntentCache.reload()`).

Per-call state (which responses have been used so we don't repeat) lives
in `IntentCallState`, attached to an `IrisAgent` instance — one per call.
"""
from __future__ import annotations

import json
import logging
import random
import re
from pathlib import Path
from typing import Any


# Sentence splitter for prewarm. LiveKit's TTS pipeline chunks at sentence
# boundaries (. / ? / !) before each chunk is looked up in the audio cache.
# So prerendering must produce the SAME chunks the speak path will request —
# otherwise the cache hits on the multi-sentence text we stored never fire
# (we stored 1 entry, the speak path looks up 2 sub-entries).
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def split_into_chunks(text: str) -> list[str]:
    """Split `text` into the same chunks LiveKit's TTS pipeline will request.

    Empirically (see transcripts) the framework splits at end-of-sentence
    punctuation (`. ` / `? ` / `! `). The terminal punctuation stays with
    the preceding sentence; leading whitespace on the next chunk is stripped.
    Single-sentence inputs return a 1-element list (just the original text).
    """
    chunks = _SENTENCE_SPLIT_RE.split(text.strip())
    return [c.strip() for c in chunks if c.strip()]

log = logging.getLogger("intent_cache")

INTENT_CACHE_PATH = Path(__file__).parent / "intent_cache.json"


# =============================================================================
# Per-call state
# =============================================================================

class IntentCallState:
    """Tracks what's been used during a single call so we don't repeat the
    same canned response twice. Reset by resetting (or replacing) the
    instance — one per call.
    """

    def __init__(self) -> None:
        self.used_response_texts: set[str] = set()
        # Set to True after any tool call fires in this conversation —
        # once we're inside a caller-specific flow (reservation lookup,
        # availability check), the cache should defer to the LLM for the
        # rest of the call.
        self.disabled: bool = False
        # Used for `skip_after_turn` guardrail.
        self.user_turn_count: int = 0


# =============================================================================
# Cache (loaded from JSON, then read-only at runtime)
# =============================================================================

class IntentCache:
    """Loaded intent definitions. Stateless — actual call state lives in
    `IntentCallState`."""

    def __init__(self, path: Path = INTENT_CACHE_PATH) -> None:
        self._path = path
        self._data: dict[str, Any] = {}
        self._sorted_triggers: list[tuple[str, str]] = []
        self.reload()

    # ----- loading -----

    def reload(self) -> None:
        """Re-read the JSON file. Safe to call at runtime if the file is edited."""
        try:
            self._data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            log.warning("intent_cache.json not found at %s — cache disabled", self._path)
            self._data = {"intents": {}, "guardrails": {}}
            self._sorted_triggers = []
            return
        except Exception as e:
            log.exception("Failed to parse %s: %s — cache disabled", self._path, e)
            self._data = {"intents": {}, "guardrails": {}}
            self._sorted_triggers = []
            return

        # Build flat list of (trigger_lower, intent_id), sorted by trigger
        # length descending. Longest-match-wins means the first matching
        # trigger in this list IS the right one — short-circuit on first hit.
        # Intent IDs starting with "_" are disabled (development convention:
        # rename to _<id>_DISABLED to keep the data around without firing).
        flat: list[tuple[str, str]] = []
        active_intents = {
            iid: data for iid, data in self._data.get("intents", {}).items()
            if not iid.startswith("_")
        }
        for intent_id, intent in active_intents.items():
            for kind in ("trigger_phrases", "stt_mishearings"):
                for trigger in intent.get(kind, []):
                    if isinstance(trigger, str) and trigger:
                        flat.append((trigger.lower(), intent_id))
        flat.sort(key=lambda t: len(t[0]), reverse=True)
        self._sorted_triggers = flat
        # Cache the active-intents dict so accessors don't repeatedly filter.
        self._active_intents = active_intents
        log.info(
            "Intent cache loaded: %d active intents, %d total triggers "
            "(disabled: %d)",
            len(active_intents), len(flat),
            len(self._data.get("intents", {})) - len(active_intents),
        )

    # ----- read access -----

    @property
    def intents(self) -> dict[str, Any]:
        """Active intents only — `_`-prefixed IDs are excluded as disabled."""
        return self._active_intents

    @property
    def guardrails(self) -> dict[str, Any]:
        return self._data.get("guardrails", {})

    def all_response_texts(self) -> list[str]:
        """Flat de-duplicated list of every response.text across every intent.
        These are the strings session.say() is called with."""
        seen: set[str] = set()
        out: list[str] = []
        for intent in self.intents.values():
            for resp in intent.get("responses", []):
                text = resp.get("text") if isinstance(resp, dict) else None
                if isinstance(text, str) and text and text not in seen:
                    seen.add(text)
                    out.append(text)
        return out

    def all_response_chunks(self) -> list[str]:
        """De-duplicated list of every SENTENCE inside every response.text.
        Used by the prewarm step. LiveKit chunks multi-sentence responses
        at periods before looking up the audio cache, so prerendering must
        match the chunks — not the original whole-response strings.

        e.g. response "It's twenty dollars per stay. Cats aren't allowed,
        by the way." becomes two chunks, each prewarmed separately. When
        the cache fires and session.say() is called with the full text,
        both sub-chunk lookups hit cache and there's no synthesis pause
        between sentences."""
        seen: set[str] = set()
        out: list[str] = []
        for text in self.all_response_texts():
            for chunk in split_into_chunks(text):
                if chunk and chunk not in seen:
                    seen.add(chunk)
                    out.append(chunk)
        return out

    # ----- classification -----

    def classify(self, stt_text: str, state: IntentCallState | None = None) -> str | None:
        """Match `stt_text` to an intent ID, or return None if no match
        (and the LLM should handle this turn).

        Guardrails (from `intent_cache.json.guardrails`):
        - `min_chars` / `min_words`: skip tiny inputs.
        - `skip_if_user_text_contains`: bail on words signaling a date- or
          caller-specific flow (e.g. "reservation", "availability").
        - `skip_if_recent_tool_call`: bail if `state.disabled` is True.
        - `skip_after_turn`: bail once `state.user_turn_count` is too deep.
        """
        if not stt_text:
            return None
        text_lower = stt_text.lower().strip()
        if not text_lower:
            return None

        guards = self.guardrails
        if len(text_lower) < int(guards.get("min_chars", 4)):
            return None
        if len(text_lower.split()) < int(guards.get("min_words", 2)):
            return None
        for kill_word in guards.get("skip_if_user_text_contains", []):
            if kill_word.lower() in text_lower:
                return None
        if state is not None:
            if guards.get("skip_if_recent_tool_call", True) and state.disabled:
                return None
            max_turn = int(guards.get("skip_after_turn", 12))
            if state.user_turn_count > max_turn:
                return None

        # Longest-trigger-wins. Triggers are pre-sorted by length desc,
        # so the first substring match is the answer.
        for trigger, intent_id in self._sorted_triggers:
            if trigger in text_lower:
                return intent_id
        return None

    # ----- post-action lookup -----

    def get_post_action(self, intent_id: str) -> str | None:
        """Optional `post_action` field on an intent: a string identifier
        the cache-hit path executes after speaking the response. Currently
        recognized by IrisAgent:

          - "transfer_to_front_desk": invokes self.transfer_to("front_desk")
            after speaking, then either mutes (on connected) or speaks the
            standard fallback (on no_answer).

        Returns None if the intent has no post_action, the intent doesn't
        exist, or the field isn't a string.
        """
        intent = self.intents.get(intent_id)
        if not isinstance(intent, dict):
            return None
        action = intent.get("post_action")
        if isinstance(action, str) and action:
            return action
        return None

    # ----- response picking -----

    def pick_response(
        self,
        intent_id: str,
        *,
        persona: str = "Iris",
        exclude_texts: set[str] | None = None,
    ) -> str | None:
        """Pick a response variant for `intent_id`. Filters by:
        - Persona compatibility (`responses[].personas` field, if present).
          A response with no `personas` is safe for any persona.
        - Exclusion set: don't repeat the same response within one call.
          If everything's been used, the exclusion set is bypassed (fresh
          random pick from the persona-allowed variants).

        Returns the chosen text, or None if no variant is available."""
        intent = self.intents.get(intent_id)
        if not isinstance(intent, dict):
            return None
        responses = intent.get("responses", [])
        if not responses:
            return None

        exclude_texts = exclude_texts or set()

        def persona_ok(resp: dict) -> bool:
            allowed = resp.get("personas")
            if not allowed:
                return True  # untagged → safe for any persona
            return persona in allowed

        # Primary: persona-allowed AND not-yet-used.
        candidates = [
            r for r in responses
            if isinstance(r, dict)
            and isinstance(r.get("text"), str)
            and persona_ok(r)
            and r["text"] not in exclude_texts
        ]
        # Fallback: persona-allowed (relax the dedup constraint).
        if not candidates:
            candidates = [
                r for r in responses
                if isinstance(r, dict)
                and isinstance(r.get("text"), str)
                and persona_ok(r)
            ]
        if not candidates:
            return None

        weights = [float(r.get("weight", 1.0)) for r in candidates]
        chosen = random.choices(candidates, weights=weights, k=1)[0]
        return chosen["text"]


# Module-level singleton — load once at import.
DEFAULT_CACHE = IntentCache()
