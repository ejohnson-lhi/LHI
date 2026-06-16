"""Per-call diarize watcher daemon for the Lighthouse Inn AI Reservation Agent.

Replaces the previous nightly cron with a continuously-running daemon that
processes each call's OGGs ~minutes after the call ends instead of next
morning. Designed to coexist with live calls on the same droplet —
critical, since the previous attempt to run heavy ML batches alongside
the LiveKit Agents worker dropped real customer calls due to memory
pressure during worker subprocess init.

# Coordination with live calls

The LiveKit Agents worker writes a PID-tagged flag file into
CALL_ACTIVE_DIR on entrypoint and deletes it on session shutdown. This
watcher polls that directory every CALL_POLL_INTERVAL_S seconds; on the
edge from "no calls" -> "call active" it decides:

  - If /proc/meminfo's MemAvailable >= MEM_PAUSE_THRESHOLD_MB:
    SIGSTOP the in-flight diarize subprocess. Its RSS (~3-4 GB for
    Whisper large-v3 int8 + ~500 MB for pyannote) stays resident but
    unused; the new LiveKit worker spawn fits in the remaining headroom.

  - Else: SIGKILL the diarize subprocess. Partial transcription for the
    OGG currently in progress is lost, but every already-written
    transcribed/<basename>.json is preserved. The OGG gets re-attempted
    on the next idle cycle since the diarize batch is idempotent (skips
    OGGs that already have a sibling JSON).

When all call flags clear, the watcher SIGCONTs a paused diarize (or
starts a new one if there's pending work).

# Stale-flag pruning

Workers can crash without unlinking their flag — the prune step runs
every cycle and removes flags whose PID is no longer alive. Without this
a single crashed worker would block diarize forever.

# Idle behavior

The watcher itself uses negligible memory (<50 MB RSS) and CPU (sleeps
between polls). The diarize child carries the actual work load.

# Tuning

All thresholds are env-configurable so the same code works on smaller
VMs or after a memory upgrade. Defaults are calibrated for an 8 GB
droplet running iris-backend + LiveKit worker + this watcher.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("diarize_watcher")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

HERE = Path(__file__).parent
PROJECT_ROOT = HERE.parent.parent

RECORDINGS_DIR = Path(
    os.environ.get("IRIS_RECORDINGS_DIR", PROJECT_ROOT / "recordings")
)
TRANSCRIBED_DIR = RECORDINGS_DIR / "transcribed"

# The LiveKit Agents worker writes flag files here on entrypoint and
# deletes them on session shutdown. Multiple concurrent calls are
# represented by multiple files in the directory. The watcher considers
# "calls active" = directory non-empty (after pruning stale flags).
CALL_ACTIVE_DIR = Path(
    os.environ.get("IRIS_CALL_ACTIVE_DIR", "/run/iris/active_calls")
)

DIARIZE_VENV = Path(
    os.environ.get("DIARIZE_VENV", HERE / ".venv")
)
PYTHON = DIARIZE_VENV / "bin" / "python"
DIARIZE_SCRIPT = HERE / "diarize_batch.py"

# Pause vs kill threshold. Calibrated for 8 GB droplet:
#   - Whisper large-v3 int8 RSS: ~3-4 GB
#   - pyannote embedding model: ~500 MB - 1 GB
#   - iris-backend + LiveKit framework baseline: ~1-2 GB
#   - Fresh LiveKit worker subprocess spawn for a new call: ~700 MB - 1 GB
#                                                            peak during
#                                                            prompt cache
#                                                            warmup
#   - Kernel + system: ~500 MB
# So if MemAvailable >= 1500 MB when a call lands, pausing leaves enough
# headroom for the new worker. If less, the new worker would push into
# swap — and swap thrashing is what dropped calls last time we ran heavy
# batch on this box. Kill diarize in that case, free its memory.
MEM_PAUSE_THRESHOLD_MB = int(os.environ.get("MEM_PAUSE_THRESHOLD_MB", "1500"))

# Polling cadences. Call detection needs to be fast so we react before
# the new worker is deep into init; OGG detection can be slower since
# new files only arrive at call-end pace.
CALL_POLL_INTERVAL_S = float(os.environ.get("CALL_POLL_INTERVAL_S", "2.0"))
OGG_POLL_INTERVAL_S = float(os.environ.get("OGG_POLL_INTERVAL_S", "10.0"))


def mem_available_mb() -> int | None:
    """Read /proc/meminfo's MemAvailable; return None on failure.

    MemAvailable is the kernel's estimate of new-allocation capacity
    without swapping (accounts for reclaimable cache/slab). Linux-only.
    """
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024  # kB -> MB
    except Exception:
        log.exception("Could not read /proc/meminfo")
    return None


def is_pid_alive(pid: int) -> bool:
    """Check if PID exists without sending an effective signal."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # PID exists but we can't signal it — counts as alive.
        return True
    except OSError:
        return False


def prune_stale_call_flags() -> int:
    """Remove flag files whose PID no longer exists. Returns count removed."""
    if not CALL_ACTIVE_DIR.exists():
        return 0
    removed = 0
    for f in CALL_ACTIVE_DIR.iterdir():
        # Format written by iris_agent.py: call_<pid>_<sanitized_room>
        if not f.name.startswith("call_"):
            continue
        parts = f.name.split("_", 2)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        if not is_pid_alive(pid):
            try:
                f.unlink()
                removed += 1
                log.info(
                    "Pruned stale call flag (pid=%d not alive): %s", pid, f.name,
                )
            except Exception:
                log.exception("Could not prune stale flag %s", f)
    return removed


def calls_active() -> bool:
    """True if any (non-stale) call flag is present."""
    if not CALL_ACTIVE_DIR.exists():
        return False
    for f in CALL_ACTIVE_DIR.iterdir():
        if f.name.startswith("call_"):
            return True
    return False


def find_pending_oggs() -> list[Path]:
    """Return OGGs without a sibling transcribed JSON yet."""
    if not RECORDINGS_DIR.exists():
        return []
    pending = []
    for ogg in sorted(RECORDINGS_DIR.glob("iris-call-*.ogg")):
        json_out = TRANSCRIBED_DIR / f"{ogg.stem}.json"
        if not json_out.exists():
            pending.append(ogg)
    return pending


class DiarizeProcess:
    """Wrapper around the diarize batch subprocess with pause/kill semantics.

    Holds at most one in-flight process. Caller is responsible for
    deciding when to start/pause/kill — this class just executes those
    actions and tracks state. `reap()` should be called every cycle to
    detect natural exit.
    """

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.paused: bool = False
        self.started_at: float | None = None

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> bool:
        if self.is_running():
            log.warning("start() called but a diarize is already running")
            return False
        if not PYTHON.exists():
            log.error(
                "Diarize venv python missing at %s — cannot start", PYTHON,
            )
            return False
        if not DIARIZE_SCRIPT.exists():
            log.error(
                "diarize_batch.py missing at %s — cannot start", DIARIZE_SCRIPT,
            )
            return False
        # nice -n 19: idle CPU priority. The diarize child only gets
        # cycles when nothing else wants them — Iris's worker for a new
        # call wins every contention.
        # ionice -c 3: idle I/O class. Same idea for disk. Without this,
        # the 3 GB Whisper model load can stall worker subprocess init.
        cmd = [
            "nice", "-n", "19",
            "ionice", "-c", "3",
            str(PYTHON), str(DIARIZE_SCRIPT),
        ]
        log.info("Starting diarize: %s", " ".join(cmd))
        try:
            # start_new_session=True puts the child in its own process
            # group so a SIGTERM to the watcher doesn't auto-propagate;
            # we explicitly manage the child's lifecycle.
            self.proc = subprocess.Popen(cmd, start_new_session=True)
            self.started_at = time.monotonic()
            self.paused = False
            log.info("Diarize started (pid=%d)", self.proc.pid)
            return True
        except Exception:
            log.exception("Could not start diarize subprocess")
            return False

    def pause(self) -> None:
        if not self.is_running() or self.paused:
            return
        assert self.proc is not None
        try:
            os.kill(self.proc.pid, signal.SIGSTOP)
            self.paused = True
            log.info("Paused diarize (pid=%d)", self.proc.pid)
        except Exception:
            log.exception("Could not pause diarize")

    def resume(self) -> None:
        if not self.is_running() or not self.paused:
            return
        assert self.proc is not None
        try:
            os.kill(self.proc.pid, signal.SIGCONT)
            self.paused = False
            log.info("Resumed diarize (pid=%d)", self.proc.pid)
        except Exception:
            log.exception("Could not resume diarize")

    def kill(self) -> None:
        if self.proc is None:
            return
        pid = self.proc.pid
        try:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                log.warning("Diarize (pid=%d) didn't exit after kill+5s", pid)
            log.warning(
                "Killed diarize (pid=%d) — in-progress OGG lost, "
                "completed OGGs preserved",
                pid,
            )
        except Exception:
            log.exception("Could not kill diarize")
        finally:
            self.proc = None
            self.paused = False
            self.started_at = None

    def reap(self) -> bool:
        """If process has exited, log + clear state. Returns True if reaped."""
        if self.proc is None:
            return False
        rc = self.proc.poll()
        if rc is None:
            return False
        elapsed = time.monotonic() - (self.started_at or time.monotonic())
        log.info(
            "Diarize (pid=%d) exited rc=%d after %.1fs", self.proc.pid, rc, elapsed,
        )
        self.proc = None
        self.paused = False
        self.started_at = None
        return True


def main() -> None:
    log.info("Diarize watcher starting (pid=%d)", os.getpid())
    log.info("  recordings:        %s", RECORDINGS_DIR)
    log.info("  transcribed out:   %s", TRANSCRIBED_DIR)
    log.info("  call-active flags: %s", CALL_ACTIVE_DIR)
    log.info(
        "  venv python:       %s (exists=%s)", PYTHON, PYTHON.exists(),
    )
    log.info("  pause threshold:   %d MB MemAvailable", MEM_PAUSE_THRESHOLD_MB)
    log.info("  call poll:         %.1fs", CALL_POLL_INTERVAL_S)
    log.info("  ogg poll:          %.1fs", OGG_POLL_INTERVAL_S)

    # Ensure the flag directory exists. systemd's RuntimeDirectory= takes
    # care of this when running as a service, but standalone invocation
    # (manual testing) still needs the mkdir.
    try:
        CALL_ACTIVE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        log.exception(
            "Could not mkdir %s — pause/kill coordination disabled, "
            "diarize will run unpaused",
            CALL_ACTIVE_DIR,
        )

    diarize = DiarizeProcess()
    prev_calls_active = False
    last_ogg_poll = 0.0

    shutting_down = False

    def _sigterm(signum, frame):
        nonlocal shutting_down
        log.info("Received signal %d, shutting down", signum)
        shutting_down = True

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    while not shutting_down:
        diarize.reap()
        prune_stale_call_flags()
        active = calls_active()

        if active and not prev_calls_active:
            # Edge: call just became active. Decide pause vs kill.
            if diarize.is_running():
                mem = mem_available_mb()
                if mem is None:
                    log.warning(
                        "Call started, MemAvailable unreadable; "
                        "PAUSING diarize as safer default",
                    )
                    diarize.pause()
                elif mem < MEM_PAUSE_THRESHOLD_MB:
                    log.warning(
                        "Call started, MemAvailable=%d MB < %d MB threshold "
                        "— KILLING diarize to free memory for the worker",
                        mem, MEM_PAUSE_THRESHOLD_MB,
                    )
                    diarize.kill()
                else:
                    log.info(
                        "Call started, MemAvailable=%d MB >= %d MB threshold "
                        "— PAUSING diarize",
                        mem, MEM_PAUSE_THRESHOLD_MB,
                    )
                    diarize.pause()
            else:
                log.info("Call started; no diarize was running")
        elif not active and prev_calls_active:
            log.info("All calls ended")
            # If diarize was paused, resume. If it was killed, the next
            # idle-poll branch below will start a fresh one.
            if diarize.is_running() and diarize.paused:
                diarize.resume()
        elif active and diarize.is_running() and not diarize.paused:
            # Edge case: somehow diarize is running while a call is
            # active and not paused. Could happen if a call started
            # between cycles and the pause didn't take. Re-decide.
            mem = mem_available_mb()
            if mem is not None and mem < MEM_PAUSE_THRESHOLD_MB:
                log.warning(
                    "Diarize running unpaused while call active and "
                    "MemAvailable=%d MB < %d MB — KILL",
                    mem, MEM_PAUSE_THRESHOLD_MB,
                )
                diarize.kill()
            else:
                diarize.pause()

        prev_calls_active = active

        # Spawn a fresh batch when idle and pending work exists.
        now = time.monotonic()
        if (not diarize.is_running()
                and not active
                and now - last_ogg_poll > OGG_POLL_INTERVAL_S):
            last_ogg_poll = now
            pending = find_pending_oggs()
            if pending:
                log.info(
                    "%d pending OGG(s); starting diarize batch", len(pending),
                )
                diarize.start()

        time.sleep(CALL_POLL_INTERVAL_S)

    # Shutdown path.
    if diarize.is_running():
        log.info("Shutdown: terminating in-flight diarize")
        diarize.kill()
    log.info("Diarize watcher exited")


if __name__ == "__main__":
    main()
