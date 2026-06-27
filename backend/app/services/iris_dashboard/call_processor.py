"""Background processor that summarizes new calls and attaches the
summary to the linked Cloudbeds reservation as a note.

WORKFLOW per call:

  1. Generate the Claude summary if no sidecar exists yet.

  2. Look at the call's tool history to find a linked reservation:
       a. create_reservation succeeded -> the NEW reservation_id is linked
          AND this call is treated as the "reservation-creating" call
          for backfill purposes (step 4).
       b. else lookup_reservation succeeded -> the EXISTING reservation_id
          is linked. No backfill.
       c. else -> no reservation linkage. The summary is still cached for
          the dashboard, but nothing gets posted to Cloudbeds.

  3. Post the summary as a Cloudbeds reservationNote against the linked
     reservation IF we haven't already posted it (tracked in the summary
     sidecar's `attached_to_reservations` list -- idempotent across runs).

  4. BACKFILL: if step 2 produced a NEW reservation_id (i.e., this call
     created the booking), scan previous calls from the same caller
     phone number within the last 30 days. For each that already has
     a summary not yet attached to the new reservation, post that
     summary as an additional note. This gives the front desk the
     full conversation history that led up to the booking, not just
     the booking call itself.

WHY a background poller and not an agent shutdown_callback:

The agent process is sized for low-latency real-time call handling.
Adding an Anthropic + Cloudbeds round trip to call shutdown would:
  - Slow down the worker subprocess shutdown / release-for-next-call cycle
  - Couple summary success to the agent process (failures take down the
    call's wrap-up cleanly instead of being logged in a separate place)
  - Hit the agent's Anthropic key for a non-real-time task

Polling every N seconds from the backend is simpler, more retryable,
and keeps the agent focused. At ~60s polling interval the worst-case
delay between call-end and the front-desk note is ~60s, which is fine
for "the next time the front desk looks at the reservation."

GATING:

Set IRIS_CALL_PROCESSOR_ENABLED=false in backend .env to disable the
background task entirely. Set IRIS_CALL_PROCESSOR_INTERVAL_SECONDS to
something other than 60 to tune the polling rate.

The dashboard's manual /api/calls/{id}/regen endpoint still works
either way, so a disabled background task doesn't remove the ability
to summarize on demand.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any

from app.tools import cloudbeds
from app.services.iris_dashboard import call_index, call_summarizer

log = logging.getLogger(__name__)

# How long after a call we keep backfilling previous summaries when a
# new reservation is created. 30 days covers most "I called last week
# asking about availability, now I'm booking" scenarios. Anything
# older than this and the context is probably stale to the front desk.
BACKFILL_LOOKBACK_DAYS = 30

# Default polling interval. The agent writes transcripts at call end,
# so this is the worst-case delay between call hangup and Cloudbeds
# note appearing on the reservation. Override with
# IRIS_CALL_PROCESSOR_INTERVAL_SECONDS env.
DEFAULT_INTERVAL_SECONDS = 60


# ---------------------------------------------------------------------------
# Transcript -> reservation linkage helpers
# ---------------------------------------------------------------------------


def _parse_tool_output(raw: Any) -> dict:
    """Best-effort: parse whatever's in a FunctionCallOutput.output field
    into a dict. Returns {} on any failure (so callers can treat 'no info'
    and 'parse error' identically)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def _tool_calls_with_outputs(items: list[dict]) -> list[tuple[str, dict]]:
    """Pair each FunctionCall with the next FunctionCallOutput.

    Returns [(tool_name, output_dict), ...] in order. If a tool call has
    no matching output (e.g., call was interrupted mid-flight), it's
    skipped. The chat-history ordering invariant the framework
    guarantees -- FunctionCall always followed eventually by
    FunctionCallOutput -- means a simple stateful walk is enough.
    """
    out: list[tuple[str, dict]] = []
    pending_name: str | None = None
    for it in items:
        kind = it.get("type")
        if kind == "FunctionCall":
            pending_name = str(it.get("name") or "")
        elif kind == "FunctionCallOutput":
            if pending_name is None:
                continue
            out.append((pending_name, _parse_tool_output(it.get("output"))))
            pending_name = None
    return out


def _extract_reservation_id(output: dict) -> str | None:
    """Pull a Cloudbeds reservation_id out of a tool output dict.

    Various tool wrappers use different keys: reservation_id, reservationID,
    and (in lookup payloads) sometimes a nested reservation object.
    """
    for key in ("reservation_id", "reservationID", "id"):
        v = output.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # lookup_reservation often returns {"found": True, "reservation": {...}}
    nested = output.get("reservation")
    if isinstance(nested, dict):
        for key in ("reservation_id", "reservationID", "id"):
            v = nested.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def linked_reservation_for_call(transcript: dict) -> tuple[str | None, bool]:
    """Determine the reservation linked to a call.

    Returns (reservation_id, was_newly_created):
      - reservation_id: the Cloudbeds ID to attach the note to, or None
        if the call has no reservation linkage at all.
      - was_newly_created: True if the reservation was created during
        this call (create_reservation success). False if it was just
        looked up. False also when reservation_id is None.

    Resolution priority -- a successful create wins over a successful
    lookup. If both happened (e.g., agent looked up an existing one
    then created a fresh booking anyway), the create wins because
    that's the more recent customer intent.
    """
    items = transcript.get("items") or []
    pairs = _tool_calls_with_outputs(items)

    created_id: str | None = None
    looked_up_id: str | None = None
    for name, output in pairs:
        # Many of our tools wrap their result in {"success": True, ...}
        # or {"status": "success"}; treat either as a success signal.
        ok = (
            output.get("success") is True
            or output.get("status") in ("success", "ok")
            or _extract_reservation_id(output) is not None
        )
        if not ok:
            continue
        rid = _extract_reservation_id(output)
        if not rid:
            continue
        if name == "create_reservation":
            created_id = rid
        elif name == "lookup_reservation":
            # Only adopt as fallback; create overrides below.
            if looked_up_id is None:
                looked_up_id = rid

    if created_id:
        return (created_id, True)
    if looked_up_id:
        return (looked_up_id, False)
    return (None, False)


# ---------------------------------------------------------------------------
# Cloudbeds note formatting
# ---------------------------------------------------------------------------


def _format_note(transcript: dict, summary: dict, *, is_history: bool) -> str:
    """Format a Cloudbeds reservation note from a transcript + summary.

    The note is what the front desk reads on the reservation screen, so
    keep it scannable: short header line with date/duration, summary
    paragraph, outcome tag, any issues_observed bullets.

    is_history: True when this note is a BACKFILLED previous-call summary
    being attached to a newly-created reservation. Adds an
    "earlier call" hint to the header so the front desk can tell at
    a glance which note describes the actual booking vs the lead-up.
    """
    started_at = transcript.get("started_at") or "?"
    # Format the date for humans, not ISO.
    try:
        dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        dt = dt.astimezone(ZoneInfo("America/Los_Angeles"))
        date_str = dt.strftime("%b %d, %Y at %-I:%M %p") if hasattr(dt, "strftime") else started_at
    except (ValueError, TypeError):
        date_str = started_at

    duration = transcript.get("duration_seconds") or 0.0
    minutes = int(duration // 60)
    seconds = int(duration % 60)
    if minutes > 0:
        duration_str = f"{minutes}m {seconds:02d}s"
    else:
        duration_str = f"{seconds}s"

    label = "Iris call — earlier" if is_history else "Iris call"
    lines = [f"{label} ({date_str}, {duration_str})", ""]

    text = (summary.get("summary") or "").strip()
    if text:
        lines.append(text)
    outcome = summary.get("outcome")
    if outcome:
        lines.append("")
        lines.append(f"Outcome: {outcome}")
    issues = summary.get("issues_observed") or []
    if isinstance(issues, list) and issues:
        lines.append("")
        lines.append("Issues noted by reviewer:")
        for i in issues:
            lines.append(f"  - {i}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# State helpers — the summary sidecar tracks which Cloudbeds reservations
# it's been attached to, so we never double-post.
# ---------------------------------------------------------------------------


def _attached_list(summary: dict) -> list[str]:
    v = summary.get("attached_to_reservations") or []
    if not isinstance(v, list):
        return []
    return [str(x) for x in v if x]


def _set_attached(summary: dict, ids: list[str]) -> None:
    summary["attached_to_reservations"] = ids
    summary["attached_updated_at"] = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Previous-call discovery (for backfill on new reservation)
# ---------------------------------------------------------------------------


def _find_previous_calls_from_phone(
    caller_phone: str,
    *,
    exclude_call_id: str,
    lookback_days: int,
) -> list[call_index.CallListEntry]:
    """Return calls from the given caller phone within the lookback
    window, excluding the current call_id. Newest first.

    We reuse call_index.list_calls() so all the existing parsing /
    filtering rules apply, and just post-filter on the phone match
    and the lookback cutoff.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    cutoff_iso = cutoff.isoformat()
    out: list[call_index.CallListEntry] = []
    for entry in call_index.list_calls(limit=500):
        if entry.call_id == exclude_call_id:
            continue
        if entry.caller_phone != caller_phone:
            continue
        if entry.started_at and entry.started_at < cutoff_iso:
            continue
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Per-transcript processing
# ---------------------------------------------------------------------------


async def _ensure_summary(transcript: dict, call_id: str) -> dict | None:
    """If the summary sidecar exists, load it. Otherwise generate and write.

    Returns the loaded summary dict or None if generation failed.
    """
    sidecar = call_index.summary_sidecar_path(call_id)
    if sidecar.exists():
        try:
            return json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("Existing summary sidecar at %s is unreadable; regenerating", sidecar)
            # fall through and regenerate

    try:
        summary = await call_summarizer.summarize(transcript)
    except Exception:
        log.exception("Summarizer failed for %s", call_id)
        return None

    call_summarizer.write_sidecar(sidecar, summary)
    log.info("call_processor: generated summary for %s", call_id)
    return summary


async def _post_summary_as_note(
    reservation_id: str,
    transcript: dict,
    summary: dict,
    *,
    is_history: bool,
) -> bool:
    """Post one summary to a Cloudbeds reservation. Returns True on success."""
    note = _format_note(transcript, summary, is_history=is_history)
    try:
        result = await cloudbeds.add_reservation_note(reservation_id, note)
    except Exception:
        log.exception(
            "add_reservation_note raised unexpectedly for reservation %s",
            reservation_id,
        )
        return False
    ok = bool(result.get("success"))
    if not ok:
        log.warning(
            "Failed to post note to reservation %s: %s",
            reservation_id, result.get("error"),
        )
    return ok


def _save_summary(call_id: str, summary: dict) -> None:
    """Re-write the summary sidecar (used after we update attached list)."""
    sidecar = call_index.summary_sidecar_path(call_id)
    call_summarizer.write_sidecar(sidecar, summary)


async def _process_call(transcript_path: Path, transcript: dict) -> dict:
    """Process a single call. Idempotent -- safe to re-run.

    Returns a small status dict describing what (if anything) was done.
    """
    call_id = call_index._call_id_from_transcript(transcript)
    if call_id is None:
        return {"skipped": "no_call_id"}

    actions: list[str] = []

    # Step 1: ensure summary exists.
    summary = await _ensure_summary(transcript, call_id)
    if summary is None:
        return {"call_id": call_id, "error": "summary_generation_failed"}
    if "generated_at" in summary and summary.get("generator_version") and \
            call_id not in [t for t in actions]:
        # generated_at was just stamped if we created it; harmless if pre-existing
        pass

    # Step 2: determine the call's linked reservation.
    reservation_id, was_newly_created = linked_reservation_for_call(transcript)
    if reservation_id is None:
        actions.append("no_reservation_linked")
        return {"call_id": call_id, "actions": actions}

    # Step 3: post this call's summary if not already attached.
    attached = _attached_list(summary)
    if reservation_id not in attached:
        ok = await _post_summary_as_note(
            reservation_id, transcript, summary, is_history=False,
        )
        if ok:
            attached.append(reservation_id)
            _set_attached(summary, attached)
            _save_summary(call_id, summary)
            actions.append(f"posted_to_{reservation_id}")
        else:
            actions.append(f"failed_to_post_to_{reservation_id}")

    # Step 4: backfill previous summaries onto the new reservation.
    if was_newly_created:
        caller_phone = transcript.get("caller_phone") or ""
        if not caller_phone:
            actions.append("no_caller_phone_for_backfill")
            return {"call_id": call_id, "actions": actions}

        previous = _find_previous_calls_from_phone(
            caller_phone,
            exclude_call_id=call_id,
            lookback_days=BACKFILL_LOOKBACK_DAYS,
        )
        if not previous:
            actions.append("no_previous_calls_to_backfill")
            return {"call_id": call_id, "actions": actions}

        backfilled = 0
        for prev in previous:
            # Only backfill calls that ALREADY have a summary -- we don't
            # auto-summarize older calls just to post them. (They'll get
            # picked up on the next processor pass if they're due.)
            prev_sidecar = call_index.summary_sidecar_path(prev.call_id)
            if not prev_sidecar.exists():
                continue
            try:
                prev_summary = json.loads(prev_sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            prev_attached = _attached_list(prev_summary)
            if reservation_id in prev_attached:
                continue

            prev_transcript_path = Path(prev.transcript_path)
            try:
                prev_transcript = json.loads(prev_transcript_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

            ok = await _post_summary_as_note(
                reservation_id, prev_transcript, prev_summary, is_history=True,
            )
            if ok:
                prev_attached.append(reservation_id)
                _set_attached(prev_summary, prev_attached)
                _save_summary(prev.call_id, prev_summary)
                backfilled += 1
        if backfilled:
            actions.append(f"backfilled_{backfilled}_previous")

    return {"call_id": call_id, "actions": actions}


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------


async def process_pending() -> list[dict]:
    """One pass over the recordings dir. Returns a per-call status list.

    Safe to call from a request handler too (e.g., an admin "process now"
    button) -- it's the same code path as the background loop.
    """
    rec_dir = call_index.get_recordings_dir()
    if not rec_dir.is_dir():
        return []
    statuses: list[dict] = []
    for entry in rec_dir.iterdir():
        if not call_index._TRANSCRIPT_RE.match(entry.name):
            continue
        try:
            transcript = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("Could not read transcript %s", entry)
            continue
        try:
            result = await _process_call(entry, transcript)
        except Exception:
            log.exception("Unexpected error processing %s", entry)
            result = {"call_id": entry.name, "error": "exception"}
        # Only emit a status entry if SOMETHING happened (or errored).
        if "actions" in result or "error" in result:
            statuses.append(result)
    return statuses


async def run_loop(interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> None:
    """Run the polling loop forever. Wraps each pass in try/except so a
    transient failure doesn't take the loop down."""
    log.info(
        "call_processor: starting polling loop (interval=%ds)",
        interval_seconds,
    )
    while True:
        try:
            statuses = await process_pending()
            if statuses:
                # Only log when there's something to report -- the typical
                # idle-pass case stays quiet.
                log.info(
                    "call_processor pass: %d call(s) had activity",
                    len(statuses),
                )
                for s in statuses:
                    log.info("  %s", s)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("call_processor: unexpected loop error; continuing")
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise


def is_enabled() -> bool:
    """Honors the IRIS_CALL_PROCESSOR_ENABLED env var. Default: enabled."""
    return os.environ.get("IRIS_CALL_PROCESSOR_ENABLED", "true").strip().lower() == "true"


def interval_seconds() -> int:
    raw = os.environ.get("IRIS_CALL_PROCESSOR_INTERVAL_SECONDS", "")
    try:
        return max(15, int(raw))
    except (ValueError, TypeError):
        return DEFAULT_INTERVAL_SECONDS
