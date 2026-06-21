#!/usr/bin/env bash
# Idempotently merge KEY=VALUE lines from one .env-style file into another.
# Run on the droplet by deploy.bat after SCP'ing the Windows-side
# tools/.deploy_env to /tmp/iris_deploy_env_in.txt.
#
# Usage:
#   sync_deploy_env.sh <source> <target>
#
# For each KEY=VALUE in <source>, removes any existing line in <target>
# starting with "KEY=" and appends the new line. Preserves all other
# lines in <target>. Blank lines and # comments in <source> are skipped.
#
# Doesn't use sudo: the iris user owns /opt/iris-backend/backend/.env
# and can edit it directly.

set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "usage: $0 <source-env-file> <target-env-file>" >&2
    exit 2
fi

SRC="$1"
TGT="$2"

if [[ ! -f "$SRC" ]]; then
    echo "  source file missing: $SRC (nothing to sync)" >&2
    exit 0   # not an error — just no .deploy_env this run
fi
if [[ ! -f "$TGT" ]]; then
    echo "ERROR: target file missing: $TGT" >&2
    exit 1
fi
if [[ ! -w "$TGT" ]]; then
    echo "ERROR: target file not writable by $(whoami): $TGT" >&2
    exit 1
fi

count=0
while IFS= read -r line || [[ -n "$line" ]]; do
    # Strip trailing CR if file had Windows line endings (likely, since
    # source came from a Windows editor).
    line="${line%$'\r'}"
    # Skip blanks and comments.
    case "$line" in
        ''|'#'*) continue ;;
    esac
    # Extract KEY (everything before the first =). Reject malformed lines.
    key="${line%%=*}"
    if [[ -z "$key" || "$key" == "$line" ]]; then
        echo "  skip malformed line: $line" >&2
        continue
    fi
    # Validate key chars (env var conventions: A-Z 0-9 _). Prevents weird
    # injection via crafted source files.
    if ! [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        echo "  skip line with invalid key: $key" >&2
        continue
    fi
    # Delete any existing line for this key.
    sed -i "/^${key}=/d" "$TGT"
    # Append new value.
    echo "$line" >> "$TGT"
    count=$((count + 1))
done < "$SRC"

echo "Synced $count key(s) from $SRC into $TGT"
