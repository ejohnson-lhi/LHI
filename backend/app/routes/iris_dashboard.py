"""Internal call-review dashboard for Iris — routes + HTTP Basic auth.

URL surface:
    GET  /iris/                              dashboard HTML (single-page app)
    GET  /iris/static/{path}                 CSS/JS/etc.
    GET  /iris/api/calls                     list of recent calls (JSON)
    GET  /iris/api/calls/{call_id}           full call detail (JSON)
    POST /iris/api/calls/{call_id}/regen     regenerate summary (LLM)
    GET  /iris/api/calls/{call_id}/audio.ogg merged stereo OGG (caller=L, Iris=R)
    GET  /iris/api/calls/{call_id}/track/{n}.ogg  individual per-participant OGG

Auth: HTTP Basic. Password is settings.portal_shared_secret (the same
secret /dcs/ trusts). Username is ignored — type anything. Browser
prompts on first /iris/* request and remembers for the session.

If portal_shared_secret is empty in config, the whole router 503s
(refusing to expose recordings without auth). Set it in .env to enable.
"""
from __future__ import annotations

import base64
import logging
import secrets
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path as PathParam, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from app.config import settings
from app.services.iris_dashboard import (
    audio_merge,
    call_categorize,
    call_cost,
    call_index,
    call_summarizer,
)

log = logging.getLogger(__name__)
router = APIRouter()

# Static files (HTML/CSS/JS) live in backend/static/iris/. Resolved
# relative to the package so it works from any cwd.
_STATIC_DIR = Path(__file__).resolve().parents[2] / "static" / "iris"


# ---------------------------------------------------------------------------
# Auth: HTTP Basic, password = portal_shared_secret
# ---------------------------------------------------------------------------

_BASIC_REALM = 'Iris Dashboard'


def _unauthorized() -> HTTPException:
    """Raise 401 with the WWW-Authenticate header so browsers prompt."""
    return HTTPException(
        status_code=401,
        detail="Authentication required",
        headers={"WWW-Authenticate": f'Basic realm="{_BASIC_REALM}"'},
    )


async def require_iris_auth(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """HTTP Basic guard. Username is ignored; password must match
    settings.portal_shared_secret (constant-time compare).

    If portal_shared_secret is unset, every request gets 503 instead
    of being silently exposed. This is the same posture portal.py uses
    when its DCS guard is unconfigured.
    """
    if not settings.portal_shared_secret:
        log.warning("iris-dashboard: portal_shared_secret unset; refusing request")
        raise HTTPException(status_code=503, detail="iris dashboard not configured")

    if not authorization or not authorization.lower().startswith("basic "):
        raise _unauthorized()

    try:
        encoded = authorization.split(" ", 1)[1]
        decoded = base64.b64decode(encoded).decode("utf-8", errors="replace")
        # decoded is "username:password"
        _, _, password = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        raise _unauthorized()

    if not secrets.compare_digest(password, settings.portal_shared_secret):
        raise _unauthorized()


# ---------------------------------------------------------------------------
# Static + HTML
# ---------------------------------------------------------------------------

@router.get("/iris/", response_class=HTMLResponse,
            dependencies=[Depends(require_iris_auth)])
async def dashboard_root() -> HTMLResponse:
    """Serve the single-page app HTML.

    Everything else (lists, details, summary regen) is fetched via the
    JSON API endpoints below. The HTML is a tiny shell that loads
    static/iris/app.js and renders into a #root div.
    """
    html_path = _STATIC_DIR / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=500, detail="iris dashboard HTML missing")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@router.get("/iris/static/{path:path}",
            dependencies=[Depends(require_iris_auth)])
async def dashboard_static(path: str) -> FileResponse:
    """Serve static assets (CSS, JS) from backend/static/iris/.

    Path traversal protection: we resolve the requested path against the
    static dir and verify the result is still inside it. Any attempt to
    escape with ../ gets a 404.
    """
    target = (_STATIC_DIR / path).resolve()
    try:
        target.relative_to(_STATIC_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=404, detail="not found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    # FileResponse picks media type from extension. That handles CSS/JS
    # fine; if we ever serve binary assets here we'd set media_type.
    return FileResponse(target)


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@router.get("/iris/api/calls",
            dependencies=[Depends(require_iris_auth)])
async def api_list_calls(limit: int = 200) -> JSONResponse:
    """List recent calls, newest first.

    Each entry includes the cheap derived fields (call_id, started_at,
    caller_phone, duration, item_count, has_summary, has_merged_audio).
    Cost and categories are NOT in the list view (they require parsing
    full events) — fetch them in /api/calls/{call_id} on click.
    """
    limit = max(1, min(int(limit), 500))
    entries = call_index.list_calls(limit=limit)
    return JSONResponse({
        "calls": [
            {
                "call_id": e.call_id,
                "started_at": e.started_at,
                "caller_phone": e.caller_phone,
                "duration_seconds": e.duration_seconds,
                "item_count": e.item_count,
                "has_summary": e.has_summary,
                "has_merged_audio": e.has_merged_audio,
            }
            for e in entries
        ]
    })


def _validate_call_id(call_id: str) -> str:
    """Reject anything that isn't a plausible call_id.

    Defense in depth against path-traversal — call_index also restricts
    to its own naming scheme, but we re-check here so anything weird
    short-circuits at the route boundary.
    """
    if not call_id.startswith("iris-call-") or "/" in call_id or "\\" in call_id:
        raise HTTPException(status_code=404, detail="call not found")
    if len(call_id) > 200:
        raise HTTPException(status_code=400, detail="call_id too long")
    return call_id


@router.get("/iris/api/calls/{call_id}",
            dependencies=[Depends(require_iris_auth)])
async def api_get_call(
    call_id: Annotated[str, PathParam()],
) -> JSONResponse:
    """Full call detail: transcript, summary (if cached), cost, categories."""
    call_id = _validate_call_id(call_id)
    detail = call_index.get_call(call_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="call not found")

    # Cost + categories from transcript (deterministic, cheap).
    transcript = {
        "events": detail.events,
        "items": detail.items,
        "duration_seconds": detail.duration_seconds,
    }
    cost = call_cost.calculate_cost(transcript, sms_count=0).to_dict()
    categories = call_categorize.categorize(transcript)

    return JSONResponse({
        "call_id": detail.call_id,
        "started_at": detail.started_at,
        "ended_at": detail.ended_at,
        "caller_phone": detail.caller_phone,
        "duration_seconds": detail.duration_seconds,
        "item_count": detail.item_count,
        "event_count": detail.event_count,
        "events": detail.events,
        "items": detail.items,
        "tts_cache_stats": detail.tts_cache_stats,
        "prewarm_stats": detail.prewarm_stats,
        "tracks": [
            {
                "track_id": t.track_id,
                "identity": t.identity,
                "role": t.role,
                "label": t.label,
            }
            for t in detail.tracks
        ],
        "has_merged_audio": detail.merged_audio_path is not None,
        "categories": categories,
        "cost": cost,
        "summary": detail.summary,  # None if not yet generated
    })


@router.post("/iris/api/calls/{call_id}/regen",
             dependencies=[Depends(require_iris_auth)])
async def api_regen_summary(
    call_id: Annotated[str, PathParam()],
) -> JSONResponse:
    """Regenerate the Claude summary for a call and write to sidecar."""
    call_id = _validate_call_id(call_id)
    detail = call_index.get_call(call_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="call not found")

    transcript = {
        "started_at": detail.started_at,
        "duration_seconds": detail.duration_seconds,
        "items": detail.items,
    }
    try:
        summary = await call_summarizer.summarize(transcript)
    except Exception as e:
        log.exception("Summarizer failed for %s", call_id)
        raise HTTPException(status_code=502, detail=f"summarizer failed: {e}")

    sidecar = call_index.summary_sidecar_path(call_id)
    call_summarizer.write_sidecar(sidecar, summary)
    return JSONResponse({"ok": True, "summary": summary})


@router.get("/iris/api/calls/{call_id}/audio.ogg",
            dependencies=[Depends(require_iris_auth)])
async def api_get_merged_audio(
    call_id: Annotated[str, PathParam()],
    swap: int = 0,
) -> Response:
    """Serve the role-aware stereo-merged OGG.

    Channel assignment:
      - LEFT: tracks tagged "caller"
      - RIGHT: tracks tagged "iris" + "answerer" mixed (these never
        overlap in time in our call flow, so the right channel plays
        the AI during the agent portion and the human during transfer)
      - swap=1 flips LEFT <-> RIGHT

    Returns 500 if the merge fails. The frontend falls back to
    per-track playback in that case (each track listed with its label).
    """
    call_id = _validate_call_id(call_id)
    detail = call_index.get_call(call_id)
    if detail is None or not detail.tracks:
        raise HTTPException(status_code=404, detail="call has no audio")

    out_path = call_index.merged_audio_path(call_id)
    track_dicts = [
        {"path": t.path, "role": t.role, "track_id": t.track_id}
        for t in detail.tracks
    ]
    ok = await audio_merge.merge_to_stereo(
        track_dicts, out_path, swap_channels=bool(swap), force=bool(swap),
    )
    if not ok:
        raise HTTPException(status_code=500, detail="audio merge failed")

    return FileResponse(out_path, media_type="audio/ogg")


@router.get("/iris/api/calls/{call_id}/track/{idx}.ogg",
            dependencies=[Depends(require_iris_auth)])
async def api_get_track(
    call_id: Annotated[str, PathParam()],
    idx: int,
) -> Response:
    """Serve an individual per-participant OGG by index.

    Used as a fallback when the stereo merge fails or when the user
    wants to hear one party in isolation. Index corresponds to the
    `tracks` array in the call detail response.
    """
    call_id = _validate_call_id(call_id)
    detail = call_index.get_call(call_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="call not found")
    if not (0 <= idx < len(detail.tracks)):
        raise HTTPException(status_code=404, detail="track not found")
    return FileResponse(detail.tracks[idx].path, media_type="audio/ogg")
