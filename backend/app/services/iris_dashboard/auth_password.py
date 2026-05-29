"""Password storage + verification for the Iris dashboard.

WHY this exists separately from settings.portal_shared_secret:

The DCS relay (/dcs/*) keeps using settings.portal_shared_secret from
.env. That value is set at deploy time and shouldn't be touched by a
casual UI action. The iris dashboard, however, is an interactive tool
where the owner reasonably wants to change the password from the
website without SSH-ing in to edit .env and restart systemd.

So we keep two credentials:
  - PORTAL_SHARED_SECRET (from .env): used by DCS relay. Static.
  - Iris dashboard password: stored in a JSON file under the backend's
    writable data dir. Initially defaults to PORTAL_SHARED_SECRET so
    the dashboard is reachable on first launch; once the owner sets
    a custom password via /iris/api/change-password, the file's
    hashed value is the source of truth.

Storage format: JSON with pbkdf2_sha256-hashed password + salt. No
external deps — stdlib's hashlib.pbkdf2_hmac is good enough for a
single-user shared secret with ~200k iterations.

Concurrency: only one writer (the FastAPI worker handling the password
change). Reads are tiny and infrequent. We don't bother with a lock --
worst case an in-flight change collides with a concurrent verify,
which would 401 the verifier and the user retries.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Where the hashed password lives. Backend's data dir is already in
# the systemd unit's ReadWritePaths, so this is the only place the
# uvicorn user can reliably write to (besides /opt/iris-backend/recordings,
# but that's for call artifacts -- credentials don't belong there).
_PASSWORD_FILE_DEFAULT = Path("/opt/iris-backend/backend/data/iris_dashboard_password.json")


def _password_file() -> Path:
    return Path(os.environ.get("IRIS_DASHBOARD_PASSWORD_FILE", str(_PASSWORD_FILE_DEFAULT)))


# pbkdf2 cost. 200k iterations of sha256 on a modern CPU is ~150ms --
# fast enough for an interactive auth check, slow enough to make
# brute-force pointless against any reasonable password.
PBKDF2_ITERATIONS = 200_000
SALT_BYTES = 32

# Password policy. Generous because this is an internal tool used by
# one person; we just want to prevent "123" being accepted.
MIN_PASSWORD_LENGTH = 10


def _hash_password(password: str, salt: bytes) -> bytes:
    """pbkdf2_hmac with sha256. Returns the 32-byte derived key."""
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
        dklen=32,
    )


def _read_stored() -> dict | None:
    """Load the hashed-password record, or None if no custom password
    has been set yet (falls back to PORTAL_SHARED_SECRET in that case)."""
    path = _password_file()
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        log.exception("Could not read %s; falling back to .env password", path)
        return None


def _write_stored(record: dict) -> None:
    """Atomically write the hashed-password record."""
    path = _password_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    os.replace(tmp, path)
    # Restrict to the owner. Backend service runs as `iris`; we don't
    # want any other user on the droplet to read this even though it's
    # already hashed.
    try:
        path.chmod(0o600)
    except OSError:
        log.warning("Could not chmod %s to 600", path)


def verify(candidate: str, *, env_fallback: str | None = None) -> bool:
    """True iff `candidate` matches the current dashboard password.

    Resolution order:
      1. Hashed record at _password_file() if it exists -- the user
         set a custom password via the UI.
      2. env_fallback string-compare -- typically settings.portal_shared_secret
         from .env. Used until the user sets a custom password.

    Returns False if both sources are unset (no auth configured at all).
    """
    if not candidate:
        return False

    record = _read_stored()
    if record is not None:
        try:
            salt = base64.b64decode(record["salt_b64"])
            stored_hash = base64.b64decode(record["hash_b64"])
        except (KeyError, ValueError, TypeError):
            log.exception("Stored password record is malformed; falling back")
            record = None
        else:
            candidate_hash = _hash_password(candidate, salt)
            # Constant-time compare; failure or success, same time.
            return hmac.compare_digest(candidate_hash, stored_hash)

    if env_fallback:
        # No stored hash yet -- use the .env value as initial password.
        return secrets.compare_digest(candidate, env_fallback)

    return False


def is_custom_set() -> bool:
    """True if the owner has set a custom password (i.e., we're not
    still using PORTAL_SHARED_SECRET as the dashboard credential)."""
    return _read_stored() is not None


def set_password(new_password: str) -> None:
    """Persist a new password. Caller is responsible for verifying
    the current password and validating policy first.

    Raises ValueError if the password fails the minimum-length check
    (defense in depth -- the route layer should also enforce this).
    """
    if not isinstance(new_password, str) or len(new_password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
        )

    salt = secrets.token_bytes(SALT_BYTES)
    derived = _hash_password(new_password, salt)
    record = {
        "algorithm": "pbkdf2_hmac_sha256",
        "iterations": PBKDF2_ITERATIONS,
        "salt_b64": base64.b64encode(salt).decode("ascii"),
        "hash_b64": base64.b64encode(derived).decode("ascii"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_stored(record)
    log.info("Dashboard password updated; record at %s", _password_file())
