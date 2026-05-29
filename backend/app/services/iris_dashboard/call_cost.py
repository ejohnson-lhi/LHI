"""Deterministic per-call cost calculation from transcript metrics.

Reads `events` array from a transcript and sums LLM, STT, and inferred
Twilio/SMS costs. No LLM, no I/O — pure number crunching.

ALL PRICES ARE BEST-EFFORT ESTIMATES from public pricing pages as of
early 2026. They're maintained as constants below; update them when
the vendors change pricing. The dashboard is for our own evaluation
so a few percent of accuracy is fine — we mainly want to know "is
this call $0.05 or $0.50?"
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import Any

log = logging.getLogger(__name__)


# Anthropic Claude Sonnet 4.5 pricing per 1M tokens. Last verified Jan 2026
# from anthropic.com/pricing. If they shift, update here.
ANTHROPIC_PRICE_PER_M = {
    "input": 3.00,          # uncached input tokens
    "cache_write": 3.75,    # writing a new cache prefix (25% premium over input)
    "cache_read": 0.30,     # reading a cached prefix (90% discount vs input)
    "output": 15.00,        # completion tokens
}

# Deepgram Nova-3 pre-recorded/streaming pricing per minute of audio.
# We use the streaming rate since Iris runs STT live. ~$0.0058/min.
DEEPGRAM_PER_MINUTE = 0.0058

# Twilio inbound voice (PSTN -> Elastic SIP Trunk). At Lighthouse's volume
# tier this is approximately $0.0085/min for inbound to the DID +
# $0.005/min for the SIP Trunk leg. Combined ~$0.0135/min. Round up to
# the nearest whole minute per Twilio's billing.
TWILIO_INBOUND_PER_MINUTE = 0.0135

# Twilio SMS rate per outbound A2P 10DLC segment. ~$0.0079 per segment.
TWILIO_SMS_PER_SEGMENT = 0.0079


@dataclass
class CostBreakdown:
    """Per-bucket costs in USD. All non-negative; zero if not applicable."""
    llm_input_uncached_usd: float = 0.0
    llm_cache_write_usd: float = 0.0
    llm_cache_read_usd: float = 0.0
    llm_output_usd: float = 0.0
    llm_total_usd: float = 0.0
    stt_total_usd: float = 0.0
    stt_seconds: float = 0.0
    twilio_minutes_usd: float = 0.0
    twilio_minutes: float = 0.0
    sms_usd: float = 0.0
    sms_count: int = 0
    total_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _estimate_llm_cost(events: list[dict]) -> tuple[float, float, float, float]:
    """Estimate LLM cost buckets from metrics.LLMMetrics events.

    The transcript records `prompt_tokens`, `prompt_cached_tokens`, and
    `completion_tokens` per LLM call. From those:
        cache_read_tokens     = prompt_cached_tokens
        non_cached_in_prompt  = prompt_tokens - prompt_cached_tokens
        completion_tokens     = completion_tokens

    We can't tell from these alone whether a non-cached portion was a
    cache WRITE (new prefix being inserted into the cache) or just
    fresh input that ran through normally. As a reasonable heuristic:
        Turn 1 of a call: assume the non-cached portion was cache write.
        Turn 2+: split it between write and pure input based on a
        delta from the previous turn's prompt_tokens.

    Errors here are bounded (cache_write is 25% premium over input;
    even if we misclassify the whole prompt, the relative cost only
    moves by 25%). Good enough for a "what did this call cost"
    dashboard.
    """
    llm_events = [e for e in events if e.get("event") == "metrics.LLMMetrics"]
    if not llm_events:
        return (0.0, 0.0, 0.0, 0.0)

    cache_read_tokens = 0
    cache_write_tokens = 0
    input_tokens = 0
    output_tokens = 0

    prev_prompt_tokens = 0
    prev_cached_tokens = 0
    for i, ev in enumerate(llm_events):
        prompt_tokens = int(ev.get("prompt_tokens") or 0)
        cached_tokens = int(ev.get("prompt_cached_tokens") or 0)
        completion = int(ev.get("completion_tokens") or 0)
        non_cached = max(prompt_tokens - cached_tokens, 0)

        cache_read_tokens += cached_tokens
        output_tokens += completion

        if i == 0:
            # First turn: the non-cached portion IS the cache write
            # (we're writing tools+system to the cache for the first time).
            cache_write_tokens += non_cached
        else:
            # Subsequent turns: the cached portion grew (or stayed same).
            # Anything added vs the previous turn was likely cache-write;
            # the rest was input that didn't get cached (typically the
            # latest user message).
            cache_growth = max(cached_tokens - prev_cached_tokens, 0)
            new_input = max(non_cached - cache_growth, 0)
            cache_write_tokens += cache_growth
            input_tokens += new_input

        prev_prompt_tokens = prompt_tokens
        prev_cached_tokens = cached_tokens

    in_uncached = input_tokens / 1_000_000.0 * ANTHROPIC_PRICE_PER_M["input"]
    cw = cache_write_tokens / 1_000_000.0 * ANTHROPIC_PRICE_PER_M["cache_write"]
    cr = cache_read_tokens / 1_000_000.0 * ANTHROPIC_PRICE_PER_M["cache_read"]
    out = output_tokens / 1_000_000.0 * ANTHROPIC_PRICE_PER_M["output"]
    return (in_uncached, cw, cr, out)


def _estimate_stt_cost(events: list[dict]) -> tuple[float, float]:
    """Estimate STT cost from metrics.STTMetrics events.

    audio_duration is reported in seconds per chunk processed. Sum them
    all and convert to per-minute pricing. This double-counts if the
    framework emits multiple STT events for the same audio segment
    (e.g., on retries) but that's rare; treat it as an upper bound.
    """
    stt_events = [e for e in events if e.get("event") == "metrics.STTMetrics"]
    total_seconds = sum(float(e.get("audio_duration") or 0.0) for e in stt_events)
    minutes = total_seconds / 60.0
    cost = minutes * DEEPGRAM_PER_MINUTE
    return (cost, total_seconds)


def _estimate_twilio_minutes_cost(duration_seconds: float) -> tuple[float, float]:
    """Twilio bills inbound calls per-minute, rounding up.

    Total cost is ceil(seconds/60) * per-minute rate. We use ceil because
    Twilio's billing does.
    """
    import math
    minutes_billed = math.ceil(max(duration_seconds, 1.0) / 60.0)
    cost = minutes_billed * TWILIO_INBOUND_PER_MINUTE
    return (cost, float(minutes_billed))


def calculate_cost(transcript: dict, sms_count: int = 0) -> CostBreakdown:
    """Compute a full CostBreakdown for one call from its transcript JSON.

    The sms_count is passed in because we don't track sent-during-call
    SMS in the transcript itself; the caller (route layer) joins it from
    the backend's twilio_sms log if available. Default 0 -- no SMS cost.
    """
    events = transcript.get("events") or []
    duration = float(transcript.get("duration_seconds") or 0.0)

    in_uncached, cw, cr, out = _estimate_llm_cost(events)
    llm_total = in_uncached + cw + cr + out

    stt_cost, stt_seconds = _estimate_stt_cost(events)
    twilio_cost, twilio_minutes = _estimate_twilio_minutes_cost(duration)
    sms_cost = sms_count * TWILIO_SMS_PER_SEGMENT

    total = llm_total + stt_cost + twilio_cost + sms_cost
    return CostBreakdown(
        llm_input_uncached_usd=round(in_uncached, 5),
        llm_cache_write_usd=round(cw, 5),
        llm_cache_read_usd=round(cr, 5),
        llm_output_usd=round(out, 5),
        llm_total_usd=round(llm_total, 5),
        stt_total_usd=round(stt_cost, 5),
        stt_seconds=round(stt_seconds, 2),
        twilio_minutes_usd=round(twilio_cost, 5),
        twilio_minutes=twilio_minutes,
        sms_usd=round(sms_cost, 5),
        sms_count=sms_count,
        total_usd=round(total, 5),
    )
