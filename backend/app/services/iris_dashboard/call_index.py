"""Filesystem -> call list / call detail.

The agent writes three kinds of artifacts to `recordings/` per call:
  1. `transcript_YYYYMMDD_HHMMSS_{caller_phone}.json` — the event timeline,
     chat history, metrics, prewarm stats. Source of truth for everything
     non-audio. The "room" field inside it is our canonical call_id.
  2. `iris-call-..._TR_{track_id}-...ogg` — one per published track
     (typically two: the caller's SIP track and the agent's TTS track).
  3. `EG_*.json` — LiveKit egress metadata, one per recorded track. Has
     egress_id, room_name, track_id, started_at, ended_at.

This module reads those files cold each time (no DB) and assembles a
list/detail view. Cheap because the typical directory size is hundreds
of files, not millions. If it ever gets slow we can add a sqlite index;
until then, plain os.scandir is fine.

The summarizer's sidecar JSON (`summary_{call_id}.json`) is read here
too if present, but generating it is the summarizer's job (separate
module so we don't pull anthropic on a cold list view).
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# Recordings dir comes from env / config. On the droplet this is
# /opt/iris-backend/recordings; in dev it's whatever IRIS_RECORDINGS_DIR
# points at. Resolved lazily so test code can monkeypatch.
def _recordings_dir() -> Path:
    p = Path(os.environ.get("IRIS_RECORDINGS_DIR", "/opt/iris-backend/recordings"))
    return p


# Transcript filenames look like:
#   transcript_20260529_184450_15419973221.json
# The trailing digits are the caller phone with the leading + stripped.
_TRANSCRIPT_RE = re.compile(
    r"^transcript_(?P<ts>\d{8}_\d{6})_(?P<phone>\d+)\.json$"
)

# OGG filenames come in two flavors, depending on when the call was recorded:
#
#   Old (pre-dispatch-rule-update on 2026-05-29):
#     iris-call-_+15419973221_NRfjPAm6ksFs-TR_AMsSSGfVtkJZB5-2026-05-29T184450.ogg
#     Pattern: {room_name}-TR_{track_id}-{time}.ogg
#     No publisher_identity. We label these tracks "Track A", "Track B" --
#     no automatic role guess.
#
#   New (post-dispatch-rule-update):
#     iris-call-_+15419973221_NRfjPAm6ksFs-agent-AB12-TR_AMsSSGfVtkJZB5-2026-05-29T184450.ogg
#     Pattern: {room_name}-{publisher_identity}-TR_{track_id}-{time}.ogg
#     publisher_identity tells us caller vs iris vs answerer (see
#     identity_to_role below).
#
# The new format is matched first; if that fails we fall back to the
# old format. Either way we end up with track_id + optional identity.
_OGG_NEW_RE = re.compile(
    r"^(?P<call_id>iris-call-.+?)-(?P<identity>[^-]+(?:-[^-]+)*?)-TR_(?P<track>[A-Za-z0-9]+)"
    r"-(?P<ts>\d{4}-\d{2}-\d{2}T\d{6})\.ogg$"
)
_OGG_OLD_RE = re.compile(
    r"^(?P<call_id>iris-call-.+?)-TR_(?P<track>[A-Za-z0-9]+)"
    r"-(?P<ts>\d{4}-\d{2}-\d{2}T\d{6})\.ogg$"
)


def _parse_ogg_name(filename: str, expected_call_id: str) -> dict | None:
    """Parse an OGG filename, returning {track_id, identity, ts} or None.

    Tries the new format first (with publisher_identity), then the old.
    Returns None if neither matches OR if the parsed call_id doesn't
    match the expected one (so we don't accidentally pull adjacent
    calls' files in).
    """
    m = _OGG_NEW_RE.match(filename)
    if m and m.group("call_id") == expected_call_id:
        return {
            "track_id": m.group("track"),
            "identity": m.group("identity"),
            "ts": m.group("ts"),
        }
    m = _OGG_OLD_RE.match(filename)
    if m and m.group("call_id") == expected_call_id:
        return {
            "track_id": m.group("track"),
            "identity": None,
            "ts": m.group("ts"),
        }
    return None


def identity_to_role(identity: str | None) -> tuple[str, str]:
    """Map a LiveKit publisher_identity to (role, display_label).

    Roles are stable enum-ish strings the frontend can switch on:
      "caller"    Inbound PSTN caller via Twilio/SIP
      "iris"      The AI agent (LiveKit Agents worker)
      "answerer"  Outbound SIP participant joined via transfer_to
      "unknown"   Identity didn't match any known pattern

    The labels are user-facing display strings, fine to change without
    breaking anything.

    Identity prefix conventions (set by our code or LiveKit):
      transfer-front_desk        -> front-desk transfer destination
      transfer-front_desk_port2  -> production port-2 front desk (Port 2)
      transfer-eric              -> Eric's cell
      agent-*                    -> Iris (LiveKit Agents auto-assigns
                                   "agent-{uuid}" by default)
      sip_*                      -> SIP-side caller (LiveKit-SIP convention
                                   for inbound participants)
      iris*                      -> Iris if we ever explicitly set it

    Anything else is "unknown" -- the dashboard will still surface it,
    just without a role tag.
    """
    if not identity:
        return ("unknown", "Unknown")
    low = identity.lower()
    if low.startswith("transfer-"):
        if "eric" in low:
            return ("answerer", "Eric")
        if "port2" in low:
            return ("answerer", "Front Desk (prod)")
        return ("answerer", "Front Desk")
    if low.startswith("agent") or low.startswith("iris"):
        return ("iris", "Iris")
    if low.startswith("sip"):
        return ("caller", "Caller")
    # Bare phone number (some SIP setups use the caller phone as identity)
    if low.lstrip("+").isdigit():
        return ("caller", "Caller")
    return ("unknown", identity)


@dataclass
class CallListEntry:
    """Per-call info for the list view. Fields are populated as cheaply as
    possible -- we read each transcript once and run the deterministic
    cost + categorize passes inline so the list shows them without an
    extra round trip per row.

    Derived fields:
      categories: rule-based tags from call_categorize.categorize().
      outcome: from cached summary's "outcome" field if available; falls
               back to the most specific category tag otherwise.
      summary_short: ~120 chars of the cached summary if available, else
                     a one-line stub describing the call shape.
      cost_total_usd: deterministic total from call_cost.calculate_cost.
    """
    call_id: str
    transcript_path: str
    started_at: str  # ISO format
    caller_phone: str
    duration_seconds: float
    item_count: int
    has_summary: bool
    has_merged_audio: bool
    categories: list[str] = field(default_factory=list)
    outcome: str | None = None
    summary_short: str | None = None
    cost_total_usd: float = 0.0


@dataclass
class Track:
    """One per-participant audio track recorded for a call."""
    path: str
    track_id: str
    identity: str | None  # None for old (pre-2026-05-29) recordings
    role: str             # "caller" | "iris" | "answerer" | "unknown"
    label: str            # Display label, e.g. "Caller" / "Iris" / "Front Desk"


@dataclass
class CallDetail:
    """Full per-call data for the detail view. Includes raw transcript."""
    call_id: str
    transcript_path: str
    started_at: str
    ended_at: str
    caller_phone: str
    duration_seconds: float
    item_count: int
    event_count: int
    events: list[dict]
    items: list[dict]
    tts_cache_stats: dict | None
    prewarm_stats: dict | None
    tracks: list[Track] = field(default_factory=list)
    merged_audio_path: str | None = None
    summary_path: str | None = None
    summary: dict | None = None


def _read_transcript(path: Path) -> dict | None:
    """Parse a transcript JSON, returning None on any failure."""
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        log.exception("Could not read transcript %s", path)
        return None


def _call_id_from_transcript(transcript: dict) -> str | None:
    """The room name written by the agent is the canonical call_id.

    Falls back to None if missing (very old recordings might not have it
    — we just skip those rather than guess).
    """
    room = transcript.get("room")
    if isinstance(room, str) and room.startswith("iris-call-"):
        return room
    return None


def _find_tracks(rec_dir: Path, call_id: str) -> list[Track]:
    """Return all per-participant tracks for a call, with parsed identity/role.

    Iterates the recordings dir once. For each OGG that belongs to this
    call (either format), we parse the filename for track_id + identity
    and derive a role. Tracks are sorted by role priority so the UI
    displays caller first, then iris, then answerer (with unknowns
    last) -- regardless of filesystem order.
    """
    role_order = {"caller": 0, "iris": 1, "answerer": 2, "unknown": 3}
    found: list[Track] = []
    # Prefilter by prefix so we don't parse every OGG in the directory.
    prefix = f"{call_id}-"
    for entry in rec_dir.iterdir():
        if not entry.name.startswith(prefix) or not entry.name.endswith(".ogg"):
            continue
        # Skip our own merged-output file -- it starts with "merged_" not the call_id.
        # (Defensive; the prefix check above already filters it out.)
        parsed = _parse_ogg_name(entry.name, call_id)
        if parsed is None:
            continue
        role, label = identity_to_role(parsed["identity"])
        found.append(Track(
            path=str(entry),
            track_id=parsed["track_id"],
            identity=parsed["identity"],
            role=role,
            label=label,
        ))
    found.sort(key=lambda t: (role_order.get(t.role, 9), t.track_id))
    return found


def _summary_sidecar_path(rec_dir: Path, call_id: str) -> Path:
    """Path where the summarizer writes its cached JSON for a call."""
    return rec_dir / f"summary_{call_id}.json"


def _merged_audio_path(rec_dir: Path, call_id: str) -> Path:
    """Path where the audio_merge module writes the stereo merged OGG."""
    return rec_dir / f"merged_{call_id}.ogg"


def _load_summary(rec_dir: Path, call_id: str) -> dict | None:
    p = _summary_sidecar_path(rec_dir, call_id)
    if not p.exists():
        return None
    try:
        with p.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        log.exception("Could not read summary %s", p)
        return None


def list_calls(limit: int = 200) -> list[CallListEntry]:
    """List recent calls, newest first.

    Reads the recordings dir, parses every transcript_*.json that matches
    the expected filename pattern, and assembles a list entry per call.
    Each entry includes the deterministic cost + rule-based categories
    so the list view can show them inline (no per-row drill-down needed).

    The `limit` is applied AFTER sorting by start time so we keep the
    most recent ones regardless of directory order.
    """
    # Local imports break a circular dep with call_cost/call_categorize
    # at module load time (they don't currently import call_index but
    # let's not paint ourselves into a corner).
    from . import call_cost, call_categorize  # noqa: PLC0415

    rec_dir = _recordings_dir()
    if not rec_dir.is_dir():
        log.warning("Recordings dir does not exist: %s", rec_dir)
        return []

    entries: list[CallListEntry] = []
    for entry in rec_dir.iterdir():
        if not _TRANSCRIPT_RE.match(entry.name):
            continue
        transcript = _read_transcript(entry)
        if transcript is None:
            continue
        call_id = _call_id_from_transcript(transcript)
        if call_id is None:
            continue
        started_at = transcript.get("started_at") or ""
        caller_phone = transcript.get("caller_phone") or ""
        duration = float(transcript.get("duration_seconds") or 0.0)
        item_count = int(transcript.get("item_count") or 0)
        has_summary = _summary_sidecar_path(rec_dir, call_id).exists()
        has_merged_audio = _merged_audio_path(rec_dir, call_id).exists()

        # Cheap deterministic derivations -- no LLM.
        categories = call_categorize.categorize(transcript)
        cost = call_cost.calculate_cost(transcript)

        # Outcome + summary preview: prefer the cached LLM summary's
        # "outcome" field if it exists; otherwise fall back to the most
        # specific category tag. summary_short is a 120-char preview
        # of the cached summary if available, else None.
        cached = _load_summary(rec_dir, call_id) if has_summary else None
        outcome = None
        summary_short = None
        if cached is not None:
            outcome = cached.get("outcome")
            s = (cached.get("summary") or "").strip()
            if s:
                summary_short = s if len(s) <= 200 else s[:197] + "..."
        if outcome is None and categories:
            # First (most-specific) category as outcome fallback.
            outcome = categories[0]

        entries.append(CallListEntry(
            call_id=call_id,
            transcript_path=str(entry),
            started_at=started_at,
            caller_phone=caller_phone,
            duration_seconds=duration,
            item_count=item_count,
            has_summary=has_summary,
            has_merged_audio=has_merged_audio,
            categories=categories,
            outcome=outcome,
            summary_short=summary_short,
            cost_total_usd=cost.total_usd,
        ))

    # Sort newest first. ISO-8601 strings sort lexicographically by time,
    # so a string compare is enough -- no need to parse datetimes.
    entries.sort(key=lambda e: e.started_at, reverse=True)
    return entries[:limit]


def get_call(call_id: str) -> CallDetail | None:
    """Load the full transcript + locate audio/summary sidecars for one call.

    Returns None if no transcript matching call_id exists. The transcript
    is read fully (events + items) because the detail view needs them all;
    if that ever gets large enough to matter, we'd paginate or stream.
    """
    rec_dir = _recordings_dir()
    if not rec_dir.is_dir():
        return None

    # Linear scan looking for the matching transcript by call_id. With
    # hundreds of files this is fine; with thousands, build an index.
    transcript: dict | None = None
    transcript_path: Path | None = None
    for entry in rec_dir.iterdir():
        if not _TRANSCRIPT_RE.match(entry.name):
            continue
        t = _read_transcript(entry)
        if t is None:
            continue
        if _call_id_from_transcript(t) == call_id:
            transcript = t
            transcript_path = entry
            break

    if transcript is None or transcript_path is None:
        return None

    tracks = _find_tracks(rec_dir, call_id)
    merged_path = _merged_audio_path(rec_dir, call_id)
    summary_path = _summary_sidecar_path(rec_dir, call_id)

    return CallDetail(
        call_id=call_id,
        transcript_path=str(transcript_path),
        started_at=transcript.get("started_at") or "",
        ended_at=transcript.get("ended_at") or "",
        caller_phone=transcript.get("caller_phone") or "",
        duration_seconds=float(transcript.get("duration_seconds") or 0.0),
        item_count=int(transcript.get("item_count") or 0),
        event_count=int(transcript.get("event_count") or 0),
        events=transcript.get("events") or [],
        items=transcript.get("items") or [],
        tts_cache_stats=transcript.get("tts_cache_stats"),
        prewarm_stats=transcript.get("prewarm_stats"),
        tracks=tracks,
        merged_audio_path=str(merged_path) if merged_path.exists() else None,
        summary_path=str(summary_path) if summary_path.exists() else None,
        summary=_load_summary(rec_dir, call_id),
    )


def get_recordings_dir() -> Path:
    """Public accessor for the recordings dir (used by audio/summary code)."""
    return _recordings_dir()


def summary_sidecar_path(call_id: str) -> Path:
    """Public accessor — summary module needs this to write the file."""
    return _summary_sidecar_path(_recordings_dir(), call_id)


def merged_audio_path(call_id: str) -> Path:
    """Public accessor — audio module writes here."""
    return _merged_audio_path(_recordings_dir(), call_id)
