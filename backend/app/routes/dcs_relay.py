"""Stable-URL relay for the hotel-side DoorCodeSetter (DCS) admin UI.

WHY: DCS runs at the hotel on port 8090 behind a router we don't control.
LAN-to-LAN routing between the dev box and other hotel LAN segments is
blocked, and staff also need to reach the admin UI from off-property.

This module exposes a stable entry point on the droplet. Two operating
modes share the same /dcs/{path} URL surface:

  PROXY MODE (preferred — set `dcs_wg_target_url` in config):
    HTTP traffic is stream-proxied through the WireGuard tunnel to the
    DCS host (no third-party SaaS in the path). The user's browser
    stays on the droplet's URL; we forward every request and stream the
    response back. Handles SSE, POST bodies, file uploads. This is the
    target architecture.

  REDIRECT MODE (legacy fallback — `dcs_wg_target_url` blank):
    DCS publishes its current ngrok URL via POST /portal/dcs-tunnel
    and /dcs/{path} 302-redirects to it. The user's browser then talks
    directly to ngrok. Kept available for months as a rollback path in
    case WireGuard misbehaves; toggle by clearing the WG target setting.

Auth model:
  - POST /portal/dcs-tunnel : X-Portal-Auth shared-secret. DCS-only.
                              Still accepted in proxy mode (heartbeats
                              are stored but not consulted for routing).
  - GET  /dcs               : landing page. Public — link IS the secret.
  - /dcs/{path}             : proxy or redirect. Public — same.
"""
import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app.config import settings
from app.routes.portal import require_portal_auth  # reuse the same guard

log = logging.getLogger(__name__)

# Hop-by-hop headers per RFC 7230 §6.1. These describe the single hop
# between client and proxy and must NOT be forwarded. "host" is also
# stripped because httpx sets it based on the upstream target URL.
_HOP_BY_HOP_HEADERS = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
})

# Module-level httpx client gives us connection pooling + TLS reuse across
# requests. timeout=Timeout(read=None) is important — SSE streams (DCS's
# /events endpoint) can stay open indefinitely. follow_redirects=False so
# the user's browser handles any redirects DCS issues (Location header
# arrives at the browser, which re-requests through our relay).
_proxy_client: httpx.AsyncClient | None = None


def _get_proxy_client() -> httpx.AsyncClient:
    global _proxy_client
    if _proxy_client is None:
        _proxy_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=None, write=15.0, pool=5.0),
            follow_redirects=False,
        )
    return _proxy_client
router = APIRouter()

# In-memory state. The current ngrok URL is ephemeral by nature (it
# rotates on every DCS restart), so persisting to DB would mostly serve
# to bridge droplet restarts. DCS heartbeats every 30s, so worst-case
# after a droplet restart is a 30-second window where /dcs/{path}
# returns 503. Acceptable; revisit if we ever see real complaints.
_state: dict = {"public_url": None, "updated_at": None}

# How long since the last DCS heartbeat before we warn users the link
# might be stale. DCS publishes every 30s; 2 minutes gives plenty of
# slack for transient network issues.
_STALE_AFTER_SECONDS = 120


class DcsTunnelPublish(BaseModel):
    public_url: str = Field(
        min_length=1,
        description="DCS's current ngrok public URL, e.g. https://abc.ngrok.app",
    )


class DcsTunnelStatus(BaseModel):
    public_url: str | None
    updated_at: datetime | None
    age_seconds: float | None


def _status_snapshot() -> DcsTunnelStatus:
    url = _state["public_url"]
    updated = _state["updated_at"]
    age = (
        (datetime.now(timezone.utc) - updated).total_seconds()
        if updated is not None
        else None
    )
    return DcsTunnelStatus(public_url=url, updated_at=updated, age_seconds=age)


@router.post(
    "/portal/dcs-tunnel",
    dependencies=[Depends(require_portal_auth)],
    response_model=DcsTunnelStatus,
)
async def publish_dcs_tunnel(req: DcsTunnelPublish) -> DcsTunnelStatus:
    """DCS reports its current ngrok tunnel URL here. Idempotent — DCS
    calls this every 30s as a heartbeat regardless of whether the URL
    changed."""
    if not req.public_url.startswith("https://"):
        raise HTTPException(400, "public_url must be an https URL")
    new_url = req.public_url.rstrip("/")
    changed = new_url != _state["public_url"]
    _state["public_url"] = new_url
    _state["updated_at"] = datetime.now(timezone.utc)
    if changed:
        log.info("DCS tunnel URL changed -> %s", new_url)
    return _status_snapshot()


@router.get(
    "/portal/dcs-tunnel",
    dependencies=[Depends(require_portal_auth)],
    response_model=DcsTunnelStatus,
)
async def get_dcs_tunnel() -> DcsTunnelStatus:
    """Inspect the currently registered DCS tunnel URL (DCS-facing diagnostic)."""
    return _status_snapshot()


@router.get("/dcs", response_class=HTMLResponse)
@router.get("/dcs/", response_class=HTMLResponse)
async def dcs_landing():
    """Small landing page with one-click access to the most-used DCS admin
    pages. The actual links proxy or redirect through `/dcs/{path}`
    depending on which mode is active."""
    proxy_mode = bool(settings.dcs_wg_target_url)
    snap = _status_snapshot()

    # In redirect mode we need a current ngrok URL or the links are dead.
    # In proxy mode the WG target URL is fixed in config, so we don't
    # require any heartbeat — the links work whenever the tunnel is up.
    if not proxy_mode and snap.public_url is None:
        return HTMLResponse(_no_tunnel_html(), status_code=503)

    stale_banner = ""
    # The stale warning only matters for redirect mode (proxy mode doesn't
    # rely on the heartbeat URL for routing).
    if not proxy_mode and snap.age_seconds is not None and snap.age_seconds > _STALE_AFTER_SECONDS:
        stale_banner = (
            f'<p style="background:#fee;border:1px solid #b00;padding:.6rem .8rem;'
            f'border-radius:.3rem;color:#900">'
            f'⚠ DCS last checked in {int(snap.age_seconds)} seconds ago. '
            f'The link may be down — try refreshing in a minute.</p>'
        )

    mode_label = "WireGuard proxy" if proxy_mode else "ngrok redirect (legacy)"
    last_iso = snap.updated_at.isoformat() if snap.updated_at else "never"
    heartbeat_line = (
        f"ngrok heartbeat (rollback path): {last_iso}"
        if proxy_mode
        else f"DCS last checked in: {last_iso}"
    )

    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>{settings.hotel_name} — Admin</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
          max-width: 30rem; margin: 2rem auto; padding: 1rem; color:#222; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 1rem; }}
  a.btn {{ display: block; margin: .6rem 0; padding: .9rem 1rem;
           background: #2b6fb6; color: white; text-decoration: none;
           border-radius: .4rem; font-weight: 600; font-size: 1.05rem; text-align:center; }}
  a.btn:hover {{ background: #1d5188; }}
  small {{ color:#888; }}
</style>
</head><body>
<h1>{settings.hotel_name} — Admin</h1>
{stale_banner}
<a class="btn" href="/dcs/HK">Housekeeping</a>
<a class="btn" href="/dcs/Reservations">Reservations</a>
<a class="btn" href="/dcs/Activity">Activity</a>
<a class="btn" href="/dcs/Schedules">Schedules</a>
<a class="btn" href="/dcs/">DCS Home</a>
<p><small>Mode: {mode_label}<br>{heartbeat_line}</small></p>
</body></html>"""
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, no-store"})


@router.api_route(
    "/dcs/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def dcs_relay(path: str, request: Request):
    """Route admin traffic to DCS. Streams over WireGuard when
    `dcs_wg_target_url` is set; otherwise 302-redirects to the most
    recent ngrok URL DCS published.

    All HTTP methods are accepted so the same path surface can carry
    webhook POSTs (Cloudbeds) and form submissions, not just GETs.
    """
    if settings.dcs_wg_target_url:
        return await _proxy_via_wireguard(request, path)
    return _redirect_via_ngrok(path)


def _redirect_via_ngrok(path: str) -> Response:
    """Legacy fallback: 302 redirect to the last-published ngrok URL."""
    url = _state["public_url"]
    if url is None:
        return HTMLResponse(_no_tunnel_html(), status_code=503)
    # Cache-Control to discourage browsers from caching the redirect —
    # the target URL rotates whenever DCS or ngrok restarts, so a cached
    # redirect would point at a dead tunnel.
    return RedirectResponse(
        f"{url}/{path}",
        status_code=302,
        headers={"Cache-Control": "no-cache, no-store"},
    )


async def _proxy_via_wireguard(request: Request, path: str) -> Response:
    """Stream-proxy the request to the WireGuard-side DCS host.

    Approach: build an httpx request that mirrors the incoming one
    (method, headers minus hop-by-hop, query params, streamed body),
    send it with stream=True, then return a StreamingResponse that
    consumes the upstream body in chunks. SSE works because we never
    buffer the response — bytes flow from DCS → droplet → client
    as the connection stays open.
    """
    target = f"{settings.dcs_wg_target_url.rstrip('/')}/{path}"

    # Build forwarded headers. Drop hop-by-hop; rewrite X-Forwarded-* so
    # DCS sees real client info for logging even though the TCP
    # connection comes from the droplet's WG IP (10.42.0.1).
    forwarded: dict[str, str] = {}
    for k, v in request.headers.items():
        if k.lower() in _HOP_BY_HOP_HEADERS:
            continue
        forwarded[k] = v
    if request.client:
        forwarded["X-Forwarded-For"] = request.client.host
    original_host = request.headers.get("host")
    if original_host:
        forwarded["X-Forwarded-Host"] = original_host
    forwarded["X-Forwarded-Proto"] = request.url.scheme

    client = _get_proxy_client()
    upstream_req = client.build_request(
        method=request.method,
        url=target,
        headers=forwarded,
        params=request.query_params,
        content=request.stream(),
    )

    try:
        upstream = await client.send(upstream_req, stream=True)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        log.warning("DCS proxy: connect failed to %s: %s", target, e)
        return HTMLResponse(
            _wg_unreachable_html(str(e)),
            status_code=503,
            headers={"Cache-Control": "no-cache, no-store"},
        )
    except httpx.HTTPError as e:
        log.exception("DCS proxy: upstream error to %s", target)
        return Response(content=f"upstream error: {e}", status_code=502, media_type="text/plain")

    # Strip hop-by-hop from upstream response. We do NOT strip
    # Content-Length: httpx's aiter_raw() yields the raw transport
    # bytes so any compression / chunking from upstream passes through
    # unchanged, and the client/uvicorn pair sorts out framing.
    response_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in _HOP_BY_HOP_HEADERS
    }

    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=response_headers,
        background=BackgroundTask(upstream.aclose),
    )


def _wg_unreachable_html(detail: str) -> str:
    safe = detail.replace("<", "&lt;").replace(">", "&gt;")
    return (
        f'<!doctype html><html><body '
        f'style="font-family:system-ui;max-width:30rem;margin:2rem auto;padding:1rem;color:#222">'
        f'<h2>{settings.hotel_name} admin not reachable</h2>'
        f"<p>The WireGuard tunnel to the hotel isn't responding. The DCS host may be offline, "
        f"the WireGuard service may be stopped, or the tunnel may be re-handshaking.</p>"
        f'<p><small>Detail: <code>{safe}</code></small></p>'
        f"</body></html>"
    )


def _no_tunnel_html() -> str:
    return (
        f'<!doctype html><html><body '
        f'style="font-family:system-ui;max-width:30rem;margin:2rem auto;padding:1rem;color:#222">'
        f'<h2>{settings.hotel_name} admin not reachable</h2>'
        f"<p>The hotel-side DoorCodeSetter app hasn't checked in with us yet, "
        f"so we don't have a current tunnel URL.</p>"
        f"<p>If DCS is running, it should publish its URL within ~30 seconds. "
        f"Try refreshing in a minute.</p>"
        f"<p>If this persists, the dev box probably can't reach the droplet — "
        f"check ngrok status on the DCS dashboard.</p>"
        f"</body></html>"
    )
