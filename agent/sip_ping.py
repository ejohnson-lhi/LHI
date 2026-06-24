"""Pre-flight SIP OPTIONS ping to a target URI.

Used by IrisAgent.on_enter to check whether the front desk SIP endpoint
is reachable WHILE Iris is speaking her greeting — caller experiences
no added latency. Result lands in self._sip_health and is consulted by
transfer_to before the 30-second ringback wait kicks off; if the ping
failed, transfer_to can short-circuit to the Phase 2 escalation
immediately instead of subjecting the caller to dead air.

A SIP OPTIONS request is the standard "are you alive" probe: same
control plane as INVITE but doesn't ring any phones. Per RFC 3261
§11, any SIP UA must respond to OPTIONS with the same status codes
it would for an equivalent INVITE — so a 200 OK means "I'd accept
a call right now" and a 4xx/5xx/timeout means we'd fail.

Wakeup side-effect: even if Twilio responds locally at its SIP edge
(rather than forwarding to the registered HT802), the activity may
nudge Twilio's registration cache to re-check the HT802 — which is
the user's specific motivation per the 2026-06-23/24 intermittent
HT802 outages.

Custom UDP implementation (no aiosip / pjsua dep) — ~120 lines, pure
stdlib (asyncio + socket). The OPTIONS request itself is ~10 SIP
headers; the only fiddly bit is getting our local outbound IP for the
Via and Contact headers so the SIP response can be routed back.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
import uuid
from typing import Any

log = logging.getLogger("sip_ping")

DEFAULT_TIMEOUT_S = 1.0
DEFAULT_PORT = 5060


def _outbound_ip_for(host: str, port: int) -> str:
    """Get the local IP the kernel would use to reach (host, port).

    Doesn't actually open a connection — UDP socket "connect" just sets
    a default peer for sendto(); no packets sent. Used to fill the Via
    header so SIP responses come back to a routable address.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, port))
        return sock.getsockname()[0]
    finally:
        sock.close()


def _parse_target(target: str) -> tuple[str, str, int]:
    """Parse a SIP target into (user_part, host, port).

    Accepts:
      - "host"                       -> ("", host, 5060)
      - "host:port"                  -> ("", host, port)
      - "user@host"                  -> (user, host, 5060)
      - "user@host:port"             -> (user, host, port)
      - "sip:user@host[:port]"       -> (user, host, port)
      - "sip:host[:port]"            -> ("", host, port)
    """
    if target.lower().startswith("sip:"):
        target = target[4:]
    if "@" in target:
        user, hostport = target.split("@", 1)
    else:
        user = ""
        hostport = target
    if ":" in hostport:
        host, port_s = hostport.rsplit(":", 1)
        try:
            port = int(port_s)
        except ValueError:
            port = DEFAULT_PORT
    else:
        host = hostport
        port = DEFAULT_PORT
    return user, host, port


def _build_options_request(
    target: str, local_host: str, local_port: int,
) -> tuple[bytes, str, int]:
    """Build a SIP OPTIONS request. Returns (bytes, target_host, target_port)."""
    user, host, port = _parse_target(target)
    target_uri = f"sip:{user}@{host}" if user else f"sip:{host}"
    branch = "z9hG4bK" + uuid.uuid4().hex[:16]
    call_id = uuid.uuid4().hex + "@" + local_host
    from_tag = uuid.uuid4().hex[:16]

    # rport in Via tells the responder to send the response back to the
    # IP/port the request actually arrived from (RFC 3581). This is the
    # standard NAT-traversal trick — without it, the responder uses the
    # IP literal in Via, which may not be reachable through our NAT.
    req = (
        f"OPTIONS {target_uri} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {local_host}:{local_port};branch={branch};rport\r\n"
        f"Max-Forwards: 70\r\n"
        f"To: <{target_uri}>\r\n"
        f'From: "Iris-Probe" <sip:probe@{local_host}>;tag={from_tag}\r\n'
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 1 OPTIONS\r\n"
        f"Contact: <sip:probe@{local_host}:{local_port}>\r\n"
        f"User-Agent: iris-sip-ping/1.0\r\n"
        f"Accept: application/sdp\r\n"
        f"Content-Length: 0\r\n"
        f"\r\n"
    )
    return req.encode("ascii"), host, port


def _parse_status_code(data: bytes) -> int | None:
    """Pull the response status code from the first line: 'SIP/2.0 200 OK'."""
    try:
        first_line = data.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
        parts = first_line.split(" ", 2)
        if len(parts) >= 2:
            return int(parts[1])
    except (ValueError, IndexError):
        pass
    return None


class _PingProtocol(asyncio.DatagramProtocol):
    """Stash the first datagram we receive into a future."""

    def __init__(self, future: asyncio.Future) -> None:
        self._future = future

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        if not self._future.done():
            self._future.set_result(data)

    def error_received(self, exc: Exception) -> None:
        if not self._future.done():
            self._future.set_exception(exc)

    def connection_lost(self, exc: Exception | None) -> None:
        if exc is not None and not self._future.done():
            self._future.set_exception(exc)


async def options_ping(
    target: str, timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Send SIP OPTIONS, wait up to timeout_s for response.

    Returns:
        {
          "target": "<as passed>",
          "reachable": bool,        # True iff any SIP response received
          "elapsed_s": float,       # round-trip time, or timeout duration
          "response_code": int|None,# parsed status code, e.g. 200, 480
          "error": str|None,        # "timeout" / exception class / None
        }
    """
    user, host, port = _parse_target(target)
    try:
        local_host = _outbound_ip_for(host, port)
    except Exception as e:
        return {
            "target": target, "reachable": False, "elapsed_s": 0.0,
            "response_code": None, "error": f"outbound_ip_failed: {e}",
        }

    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    # local_addr=("", 0) lets the kernel pick an ephemeral port.
    try:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _PingProtocol(future),
            local_addr=("", 0),
            remote_addr=(host, port),
        )
    except Exception as e:
        return {
            "target": target, "reachable": False, "elapsed_s": 0.0,
            "response_code": None, "error": f"socket_setup_failed: {e}",
        }

    try:
        # Resolve our actual ephemeral port for the Via header.
        sock = transport.get_extra_info("socket")
        local_port = sock.getsockname()[1] if sock else 0
        request_bytes, _, _ = _build_options_request(target, local_host, local_port)

        t0 = time.monotonic()
        transport.sendto(request_bytes)
        log.info("SIP OPTIONS sent to %s:%d (target=%s)", host, port, target)
        try:
            data = await asyncio.wait_for(future, timeout=timeout_s)
            elapsed = time.monotonic() - t0
            code = _parse_status_code(data)
            log.info(
                "SIP OPTIONS response from %s:%d code=%s in %.3fs",
                host, port, code, elapsed,
            )
            return {
                "target": target, "reachable": True, "elapsed_s": elapsed,
                "response_code": code, "error": None,
            }
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - t0
            log.warning(
                "SIP OPTIONS timeout to %s:%d after %.3fs", host, port, elapsed,
            )
            return {
                "target": target, "reachable": False, "elapsed_s": elapsed,
                "response_code": None, "error": "timeout",
            }
    except Exception as e:
        log.exception("SIP OPTIONS failed to %s", target)
        return {
            "target": target, "reachable": False, "elapsed_s": 0.0,
            "response_code": None, "error": f"{type(e).__name__}: {e}",
        }
    finally:
        try:
            transport.close()
        except Exception:
            pass
