#!/usr/bin/env bash
# Install the per-3h Iris smoke-test timer.
#
# Run once on the droplet after the first deploy that includes
# tools/iris_smoke_test.py + the iris-smoke-test unit files:
#
#   sudo bash /opt/iris-backend/deploy/install_smoke_test.sh
#
# Idempotent — re-runs are safe (cp -u + systemctl enable are no-ops
# when already current). What this does:
#   1. Copies iris-smoke-test.service + .timer to /etc/systemd/system/.
#   2. systemctl daemon-reload.
#   3. systemctl enable + start the .timer (the .timer schedules the
#      .service; you don't enable the .service directly).
#   4. Runs the smoke test ONCE immediately to verify it can actually
#      reach Twilio + answer Iris. If it fails here, the env vars are
#      probably not set in backend/.env yet — see the script's REQUIRED
#      list at the top of tools/iris_smoke_test.py.
#
# Future deploy.bat runs will keep the unit files in sync via cp -u in
# the SSH chain, so you only need to run THIS installer once for the
# initial enable.

set -euo pipefail

SERVICE_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/iris-smoke-test.service"
TIMER_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/iris-smoke-test.timer"
SERVICE_DST="/etc/systemd/system/iris-smoke-test.service"
TIMER_DST="/etc/systemd/system/iris-smoke-test.timer"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: must run as root (use sudo)." >&2
    exit 1
fi

for f in "$SERVICE_SRC" "$TIMER_SRC"; do
    if [[ ! -f "$f" ]]; then
        echo "ERROR: unit file missing at $f" >&2
        exit 1
    fi
done

# Sanity check on the script the units will exec.
SCRIPT="/opt/iris-backend/tools/iris_smoke_test.py"
PY="/opt/iris-backend/backend/.venv/bin/python"
if [[ ! -f "$SCRIPT" ]]; then
    echo "ERROR: $SCRIPT not present. Run deploy.bat to push the script first." >&2
    exit 1
fi
if [[ ! -x "$PY" ]]; then
    echo "ERROR: backend venv python not at $PY." >&2
    exit 1
fi

echo "=== Installing unit files ==="
cp "$SERVICE_SRC" "$SERVICE_DST"
cp "$TIMER_SRC" "$TIMER_DST"
chmod 0644 "$SERVICE_DST" "$TIMER_DST"
echo "  -> $SERVICE_DST"
echo "  -> $TIMER_DST"

echo
echo "=== Reloading systemd and enabling timer ==="
systemctl daemon-reload
systemctl enable iris-smoke-test.timer
systemctl restart iris-smoke-test.timer

echo
echo "=== Timer status ==="
systemctl --no-pager status iris-smoke-test.timer | head -10 || true
echo
echo "=== Next scheduled fires ==="
systemctl --no-pager list-timers iris-smoke-test.timer | head -5 || true

echo
echo "=== Initial smoke-test run (one-shot) ==="
echo "Triggering iris-smoke-test.service to verify Twilio creds + dev DID..."
systemctl restart iris-smoke-test.service
echo
echo "Result:"
systemctl --no-pager status iris-smoke-test.service | head -20 || true

echo
echo "Done."
echo
echo "Follow timer activity:  journalctl -u iris-smoke-test.service -f"
echo "Disable + stop:         sudo systemctl disable --now iris-smoke-test.timer"
echo
echo "If the initial run FAILED with 'Missing required env vars', edit"
echo "/opt/iris-backend/backend/.env to add the IRIS_SMOKE_TEST_* vars."
echo "See tools/iris_smoke_test.py docstring for the full required list."
