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
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path as PathParam, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from pydantic import BaseModel, Field

from app.config import settings
from app.services.iris_dashboard import (
    audio_merge,
    auth_password,
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


def _extract_basic_password(authorization: str | None) -> str | None:
    """Decode a 'Basic ...' header to its password component.

    Username is intentionally discarded -- the dashboard is single-user,
    only the password matters. Returns None on any parse failure.
    """
    if not authorization or not authorization.lower().startswith("basic "):
        return None
    try:
        encoded = authorization.split(" ", 1)[1]
        decoded = base64.b64decode(encoded).decode("utf-8", errors="replace")
        _, _, password = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        return None
    return password


async def require_iris_auth(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """HTTP Basic guard — TEMPORARILY DISABLED (2026-06-11).

    Auth is off so the transcript viewer is publicly reachable without a
    login/password prompt. The function is still referenced via
    `dependencies=[Depends(require_iris_auth)]` on every route so we can
    revert in one place: just delete the early-return below and the
    original gate (preserved verbatim, unindented) kicks back in.

    Returns a sentinel string instead of a real verified password so the
    change-password endpoint's reuse pattern doesn't crash — but that
    endpoint is also disabled below while auth is off.

    TO RE-ENABLE: delete the three lines marked AUTH-DISABLED below.
    The rest of the function body is the original gate, ready to go.
    """
    # AUTH-DISABLED ↓↓↓
    return "auth-disabled-sentinel"
    # AUTH-DISABLED ↑↑↑
    custom_set = auth_password.is_custom_set()
    if not custom_set and not settings.portal_shared_secret:
        log.warning(
            "iris-dashboard: no dashboard password set and "
            "portal_shared_secret unset; refusing request"
        )
        raise HTTPException(status_code=503, detail="iris dashboard not configured")

    password = _extract_basic_password(authorization)
    if password is None:
        raise _unauthorized()

    if not auth_password.verify(password, env_fallback=settings.portal_shared_secret):
        raise _unauthorized()

    return password


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

# ---------------------------------------------------------------------------
# Auth status + password change
# ---------------------------------------------------------------------------


class ChangePasswordRequest(BaseModel):
    """Payload for /iris/api/change-password.

    current_password must match whatever the user is logged in with
    (defense in depth -- the require_iris_auth dependency has already
    verified them, but re-checking here means the user must explicitly
    re-enter their password to authorize a change, vs an attacker who
    found an open session being able to silently rotate the password).
    """
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=auth_password.MIN_PASSWORD_LENGTH)
    confirm_password: str = Field(min_length=auth_password.MIN_PASSWORD_LENGTH)


@router.get("/iris/api/auth/status",
            dependencies=[Depends(require_iris_auth)])
async def api_auth_status() -> JSONResponse:
    """Tells the UI whether a custom password has been set yet, so
    the change-password form can be labeled correctly ("Set password"
    on first run vs "Change password" once a custom one exists)."""
    return JSONResponse({
        "custom_password_set": auth_password.is_custom_set(),
        "min_password_length": auth_password.MIN_PASSWORD_LENGTH,
    })


@router.post("/iris/api/auth/change-password")
async def api_change_password(
    body: ChangePasswordRequest,
    current_authed_password: Annotated[str, Depends(require_iris_auth)],
) -> JSONResponse:
    """Change the dashboard password.

    Defense in depth: require_iris_auth has already verified the user
    knows the current password via the browser's Basic Auth header.
    We ALSO verify body.current_password matches -- this catches the
    case where the user typed something different in the form (e.g.,
    pasted wrong text) before they update the storage.

    After success, the browser still has the OLD password cached for
    Basic Auth. The user will be prompted to re-enter on the next
    navigation/refresh. We return that hint in the response so the
    frontend can tell the user.
    """
    # AUTH-DISABLED 2026-06-11: while require_iris_auth is bypassed,
    # change-password makes no sense — there's no auth to "change". Reject
    # any attempt so a stale UI / curl can't silently rotate the stored
    # password. Remove this block when restoring require_iris_auth.
    raise HTTPException(
        status_code=503,
        detail="Password change is disabled while dashboard auth is off.",
    )
    # AUTH-DISABLED ↑↑↑ (original handler body below — preserved for revert)

    # The two confirm fields must match.
    if body.new_password != body.confirm_password:
        raise HTTPException(
            status_code=400, detail="New password and confirmation don't match",
        )
    # And current_password (from the FORM) must match what the auth header had.
    # If the user is half-way through a session, current_authed_password is
    # the password the browser is sending; body.current_password is what
    # they typed into the form. Mismatch usually means they fat-fingered.
    if body.current_password != current_authed_password:
        raise HTTPException(
            status_code=400,
            detail="Current password (form) doesn't match what you're logged in with",
        )
    if body.new_password == body.current_password:
        raise HTTPException(
            status_code=400, detail="New password must differ from current",
        )

    try:
        auth_password.set_password(body.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        log.exception("Failed to write new dashboard password")
        raise HTTPException(status_code=500, detail="could not save password")

    return JSONResponse({
        "ok": True,
        "message": (
            "Password updated. Close and reopen your browser, or do a "
            "force-refresh (Ctrl+Shift+R), and log in again with the "
            "new password."
        ),
    })


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
                "categories": e.categories,
                "outcome": e.outcome,
                "summary_short": e.summary_short,
                "cost_total_usd": e.cost_total_usd,
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
                # Diarize-batch fields (None when the nightly batch hasn't
                # processed this OGG yet). When present, the viewer
                # renders a "Post-transfer conversation" section below
                # Iris's chat history.
                "start_offset_seconds": t.start_offset_seconds,
                "diarize_segments": t.diarize_segments,
                "matched_name": t.matched_name,
                "match_score": t.match_score,
                "is_post_transfer": t.role == "answerer",
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
