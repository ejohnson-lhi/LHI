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

# OGG filenames look like:
#   iris-call-_+15419973221_NRfjPAm6ksFs-TR_AMsSSGfVtkJZB5-2026-05-29T184450.ogg
# We need the call_id prefix (everything before the first "-TR_") so we
# can glob all tracks for one call.
_OGG_RE = re.compile(
    r"^(?P<call_id>iris-call-[^-]+(?:_[^-]+)*?)-TR_(?P<track>[A-Za-z0-9]+)"
    r"-(?P<ts>\d{4}-\d{2}-\d{2}T\d{6})\.ogg$"
)


@dataclass
class CallListEntry:
    """Lightweight per-call info for the list view. Cheap to compute."""
    call_id: str
    transcript_path: str
    started_at: str  # ISO format
    caller_phone: str
    duration_seconds: float
    item_count: int
    has_summary: bool
    has_merged_audio: bool


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
    track_files: list[str] = field(default_factory=list)
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


def _find_track_oggs(rec_dir: Path, call_id: str) -> list[Path]:
    """Return all OGG files for a call, sorted by track_id for stability.

    Sorting by track_id is just to give a deterministic order in the UI.
    Which track is caller vs agent isn't encoded in the filename — the
    audio_merge module decides L/R based on a heuristic (or the user
    picks via a swap button in v2).
    """
    matches: list[tuple[str, Path]] = []
    prefix = f"{call_id}-TR_"
    for entry in rec_dir.iterdir():
        if not entry.name.startswith(prefix) or not entry.name.endswith(".ogg"):
            continue
        m = _OGG_RE.match(entry.name)
        if not m:
            continue
        matches.append((m.group("track"), entry))
    matches.sort(key=lambda x: x[0])
    return [p for _, p in matches]


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
    No LLM, no expensive computation. The `limit` is applied AFTER sorting
    by start time so we keep the most recent ones regardless of dir order.
    """
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
        # has_summary check is cheap (just a file exists); doing it here
        # so the list view can render an icon without an extra round trip.
        has_summary = _summary_sidecar_path(rec_dir, call_id).exists()
        has_merged_audio = _merged_audio_path(rec_dir, call_id).exists()
        entries.append(CallListEntry(
            call_id=call_id,
            transcript_path=str(entry),
            started_at=started_at,
            caller_phone=caller_phone,
            duration_seconds=duration,
            item_count=item_count,
            has_summary=has_summary,
            has_merged_audio=has_merged_audio,
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

    track_files = [str(p) for p in _find_track_oggs(rec_dir, call_id)]
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
        track_files=track_files,
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
