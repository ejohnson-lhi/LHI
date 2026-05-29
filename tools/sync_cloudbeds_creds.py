"""Sync Cloudbeds dashboard login credentials from local backend/.env to
the droplet's /opt/iris-backend/backend/.env, then restart iris-backend
so the new env loads. The session-cookie cache that the dashboard
endpoints need still has to be bootstrapped separately (one-shot
Playwright login); this script just plants the credentials that the
bootstrap script reads.

Keys synced (subset of those present locally):
  CLOUDBEDS_LOGIN_URL       (optional — has a default)
  CLOUDBEDS_ADMIN_EMAIL     (required for auto-login)
  CLOUDBEDS_ADMIN_PASSWORD  (required for auto-login)
  CLOUDBEDS_TOTP_SECRET     (required if the account has 2FA enrolled)

PCI/secret hygiene:
  - Values are piped through SSH stdin, never on the command line, so they
    don't appear in either side's process list or shell history.
  - Remote .env is rewritten atomically (tempfile + os.replace) with 0600
    permissions preserved.

Requirements:
  - ssh on PATH (Windows OpenSSH or equivalent)
  - Key-based auth to iris@... already working (the same one deploy.bat uses)

Usage:
  Double-click tools/sync_cloudbeds_creds.bat, or run from the repo root:
    backend/.venv/Scripts/python.exe tools/sync_cloudbeds_creds.py
"""
from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

REMOTE = "iris@64.23.167.164"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_ENV = PROJECT_ROOT / "backend" / ".env"
REMOTE_ENV_PATH = "/opt/iris-backend/backend/.env"

KEYS_TO_SYNC = (
    "CLOUDBEDS_LOGIN_URL",
    "CLOUDBEDS_ADMIN_EMAIL",
    "CLOUDBEDS_ADMIN_PASSWORD",
    "CLOUDBEDS_TOTP_SECRET",
)
REQUIRED_KEYS = ("CLOUDBEDS_ADMIN_EMAIL", "CLOUDBEDS_ADMIN_PASSWORD")


def find_ssh() -> str:
    """Locate ssh.exe on Windows — prefer the system OpenSSH location."""
    candidates = (
        r"C:\Windows\Sysnative\OpenSSH\ssh.exe",
        r"C:\Windows\System32\OpenSSH\ssh.exe",
    )
    for cand in candidates:
        if Path(cand).exists():
            return cand
    found = shutil.which("ssh")
    if not found:
        sys.exit("ERROR: ssh not found on PATH")
    return found


def extract_local_keys() -> list[tuple[str, str]]:
    """Read local .env and return (key, value) pairs for keys in KEYS_TO_SYNC,
    in the order they appear. Preserves the value's exact bytes (no stripping
    of trailing spaces, no quote-handling) — whatever is on disk locally is
    what lands on the droplet."""
    if not LOCAL_ENV.exists():
        sys.exit(f"ERROR: local .env not found at {LOCAL_ENV}")
    text = LOCAL_ENV.read_text(encoding="utf-8")
    wanted = set(KEYS_TO_SYNC)
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        k, _, v = stripped.partition("=")
        k = k.strip()
        if k in wanted:
            out.append((k, v))
    return out


# Remote merge script. Runs on the droplet under `python3 -c`. Reads
# KEY=VALUE lines from stdin, merges into REMOTE_ENV_PATH idempotently,
# writes atomically with 0600 permissions. The empty `{}` and `set()`
# literals must NOT be format placeholders, so we substitute the env path
# via plain string concat below instead of .format().
_REMOTE_MERGE_TMPL = r"""
import os, sys, tempfile
ENV = __ENV_PATH__
updates = {}
for raw in sys.stdin.read().splitlines():
    if not raw or raw.startswith('#'):
        continue
    if '=' not in raw:
        continue
    k, _, v = raw.partition('=')
    updates[k.strip()] = v
try:
    with open(ENV, 'r', encoding='utf-8') as f:
        existing = f.read().splitlines(keepends=False)
except FileNotFoundError:
    existing = []
out = []
seen = set()
for line in existing:
    stripped = line.lstrip()
    if '=' in stripped and not stripped.startswith('#'):
        key = stripped.split('=', 1)[0].strip()
        if key in updates:
            out.append(key + '=' + updates[key])
            seen.add(key)
            continue
    out.append(line)
for key, val in updates.items():
    if key not in seen:
        out.append(key + '=' + val)
text = '\n'.join(out) + '\n'
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(ENV), prefix='.env.new.')
try:
    with os.fdopen(fd, 'w', encoding='utf-8') as fout:
        fout.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, ENV)
except Exception:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
print('Updated ' + str(len(updates)) + ' key(s) in ' + ENV)
"""


def build_remote_merge_script() -> str:
    return _REMOTE_MERGE_TMPL.replace("__ENV_PATH__", repr(REMOTE_ENV_PATH))


def main() -> int:
    ssh = find_ssh()
    print(f"Local .env:   {LOCAL_ENV}")
    print(f"Remote .env:  {REMOTE}:{REMOTE_ENV_PATH}")
    print()

    pairs = extract_local_keys()
    if not pairs:
        print(f"ERROR: none of {KEYS_TO_SYNC} are in {LOCAL_ENV}")
        return 1

    found_names = {k for k, _ in pairs}
    missing_required = [k for k in REQUIRED_KEYS if k not in found_names]
    if missing_required:
        print(f"ERROR: required keys missing locally: {missing_required}")
        return 1

    print(f"Found {len(pairs)} key(s) to sync (values masked):")
    for k, v in pairs:
        if len(v) >= 4:
            shown = v[:2] + "*" * (len(v) - 4) + v[-2:]
        else:
            shown = "*" * len(v)
        print(f"  {k}={shown}")

    payload = "\n".join(f"{k}={v}" for k, v in pairs)
    remote_cmd = "python3 -c " + shlex.quote(build_remote_merge_script())

    print("\n=== Merging into droplet .env ===")
    result = subprocess.run(
        [ssh, REMOTE, remote_cmd],
        input=payload,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print("(stderr) " + result.stderr, end="" if result.stderr.endswith("\n") else "\n")
    if result.returncode != 0:
        print(f"ERROR: merge command exited {result.returncode}")
        return result.returncode

    print("\n=== Restarting iris-backend ===")
    restart_cmd = (
        "sudo systemctl restart iris-backend.service && "
        "sudo systemctl status iris-backend.service --no-pager -l | head -12"
    )
    result = subprocess.run(
        [ssh, REMOTE, restart_cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print("(stderr) " + result.stderr)
    if result.returncode != 0:
        print(f"ERROR: restart command exited {result.returncode}")
        return result.returncode

    print()
    print("=== Done ===")
    print()
    print("Next step — bootstrap the Cloudbeds session cookie cache on the droplet.")
    print("First run is best with the browser visible so you can watch:")
    print()
    print(f"  ssh {REMOTE} \\")
    print("    'cd /opt/iris-backend/backend && \\")
    print("     CLOUDBEDS_BROWSER_HEADLESS=false .venv/bin/python scripts/test_cloudbeds_login.py'")
    print()
    print("Note that headless=false on a headless droplet will fail unless you forward")
    print("X11 (ssh -X) or have a virtual display. If you don't, omit the env var to")
    print("run headless — you'll just need to trust that the script handled 2FA correctly,")
    print("and check the saved screenshots in backend/logs/ if anything looks wrong.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
