#!/usr/bin/env bash
# Install the per-call diarize watcher as a systemd service on the
# droplet. Replaces the legacy nightly cron.
#
# Run on the droplet:
#   sudo bash /opt/iris-backend/deploy/install_diarize_watcher.sh
#
# Idempotent — re-runs are safe. Disables the legacy nightly cron at
# /etc/cron.d/iris-diarize on first install (renamed with a timestamp
# suffix so it's recoverable if needed).

set -euo pipefail

UNIT_NAME="iris-diarize-watcher.service"
UNIT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/${UNIT_NAME}"
UNIT_DST="/etc/systemd/system/${UNIT_NAME}"

AGENT_UNIT="iris-agent.service"
AGENT_UNIT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/${AGENT_UNIT}"
AGENT_UNIT_DST="/etc/systemd/system/${AGENT_UNIT}"

CRON_OLD="/etc/cron.d/iris-diarize"
DIARIZE_VENV="/opt/iris-backend/tools/diarize/.venv"
DIARIZE_PY="${DIARIZE_VENV}/bin/python"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: this script must run as root (use sudo)." >&2
    exit 1
fi

if [[ ! -f "$UNIT_SRC" ]]; then
    echo "ERROR: unit file not found at $UNIT_SRC" >&2
    exit 1
fi

# Sanity check: venv must exist, otherwise the unit will Restart=on-failure
# forever and journal will fill with the same error.
if [[ ! -x "$DIARIZE_PY" ]]; then
    echo "ERROR: diarize venv python not found at $DIARIZE_PY" >&2
    echo "       Run tools/diarize/setup.sh first to create it." >&2
    exit 1
fi

echo "=== Disabling legacy nightly cron, if present ==="
if [[ -f "$CRON_OLD" ]]; then
    SUFFIX="$(date +%Y%m%d-%H%M%S)"
    BACKUP="${CRON_OLD}.disabled-${SUFFIX}"
    mv "$CRON_OLD" "$BACKUP"
    echo "  Moved $CRON_OLD -> $BACKUP"
    echo "  (delete the backup once you've confirmed the watcher works)"
else
    echo "  No legacy cron at $CRON_OLD; nothing to disable."
fi

echo
echo "=== Updating iris-agent.service (RuntimeDirectory for call flags) ==="
# The agent service was edited to declare RuntimeDirectory=iris/active_calls
# so the worker can write the call-active flag. Reinstall the updated unit.
if [[ -f "$AGENT_UNIT_SRC" ]]; then
    cp "$AGENT_UNIT_SRC" "$AGENT_UNIT_DST"
    chmod 0644 "$AGENT_UNIT_DST"
    echo "  Updated $AGENT_UNIT_DST"
else
    echo "  WARNING: $AGENT_UNIT_SRC missing — agent service not updated."
    echo "  The watcher will still work but the agent won't signal calls;"
    echo "  diarize will not pause for live calls. Fix and re-run."
fi

echo
echo "=== Installing $UNIT_NAME ==="
cp "$UNIT_SRC" "$UNIT_DST"
chmod 0644 "$UNIT_DST"
echo "  Installed $UNIT_DST"

echo
echo "=== Reloading systemd and (re)starting services ==="
systemctl daemon-reload
# Restart the agent so the RuntimeDirectory= change takes effect.
# Brief downtime — Twilio will fail any in-flight calls during this
# window, but that's the same as any agent restart.
echo "  Restarting $AGENT_UNIT (brief downtime for active calls)..."
systemctl restart "$AGENT_UNIT" || {
    echo "  WARNING: agent restart failed. Check 'journalctl -u $AGENT_UNIT'."
}
echo "  Enabling + starting $UNIT_NAME..."
systemctl enable "$UNIT_NAME"
systemctl restart "$UNIT_NAME"

echo
echo "=== Status ==="
systemctl --no-pager status "$UNIT_NAME" || true

echo
echo "=== Verifying flag dir was created ==="
if [[ -d /run/iris/active_calls ]]; then
    echo "  /run/iris/active_calls exists; mode: $(stat -c '%a %U:%G' /run/iris/active_calls)"
else
    echo "  WARNING: /run/iris/active_calls does NOT exist."
    echo "  Either the agent service hasn't restarted yet, or RuntimeDirectory"
    echo "  failed. Check 'systemctl status $AGENT_UNIT'."
fi

echo
echo "Done."
echo
echo "Follow live logs:    journalctl -u $UNIT_NAME -f"
echo "Stop:                sudo systemctl stop $UNIT_NAME"
echo "Disable (and stop):  sudo systemctl disable --now $UNIT_NAME"
