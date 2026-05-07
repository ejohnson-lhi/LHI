"""Rename downloaded audio recordings to include the call's date/time and the
correct file extension.

The original downloader had a bug where the file extension got truncated for
URLs whose path basenames were just over 80 chars, and saved them as `.bin`.
This script:

  1. Reads `calls/all_calls.json` from a given export folder
  2. For each call with a `.bin` file in `calls/audio/`, finds the call's
     `startedAt` timestamp and the correct extension from `recording_url`
  3. Renames `<basename>.bin` → `<YYYY-MM-DD>T<HH-MM-SS>_<basename>.<ext>`
     using the hotel's local Pacific time

Usage:
    python rename_recordings.py [export_folder]

If no folder is given, the most recent export under `exports/` is used.
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

EXPORTS_ROOT = Path(__file__).parent / "exports"

# Hotel is in US/Pacific. Use the timezone info Hey Sadie provides in the
# `startedAtFormatted` string (which says "PDT" or "PST" explicitly) to choose
# the correct UTC offset. Avoids a tzdata dependency on Windows.
TZ_OFFSETS = {
    "PDT": timezone(timedelta(hours=-7)),
    "PST": timezone(timedelta(hours=-8)),
    "EDT": timezone(timedelta(hours=-4)),
    "EST": timezone(timedelta(hours=-5)),
    "CDT": timezone(timedelta(hours=-5)),
    "CST": timezone(timedelta(hours=-6)),
    "MDT": timezone(timedelta(hours=-6)),
    "MST": timezone(timedelta(hours=-7)),
    "UTC": timezone.utc,
    "GMT": timezone.utc,
}
DEFAULT_TZ = TZ_OFFSETS["PST"]  # fallback if formatted string lacks a tz suffix


def utc_to_local(started_iso: str, formatted: str | None) -> datetime:
    """Convert UTC ISO timestamp to local time using the tz suffix in formatted string."""
    dt_utc = datetime.fromisoformat(started_iso.replace("Z", "+00:00"))
    local_tz = DEFAULT_TZ
    if formatted:
        m = re.search(r"\b([PEMC][SD]T|UTC|GMT)\b", formatted)
        if m and m.group(1) in TZ_OFFSETS:
            local_tz = TZ_OFFSETS[m.group(1)]
    return dt_utc.astimezone(local_tz)


def buggy_filename(url: str) -> str:
    """Replicate the original downloader's filename logic that produced .bin files."""
    tail = urlparse(url).path.split("/")[-1][:80] or "audio"
    tail = re.sub(r"[^a-zA-Z0-9._-]", "_", tail)
    if "." not in tail:
        tail += ".bin"
    return tail


def correct_extension_from_url(url: str) -> str:
    """Return the correct file extension (with leading dot) from the URL path."""
    raw = urlparse(url).path.split("/")[-1]
    if "." in raw:
        return "." + raw.rsplit(".", 1)[1][:8]
    return ".bin"


def find_export_folder(arg: str | None) -> Path:
    if arg:
        p = Path(arg)
        if not p.exists():
            print(f"ERROR: {p} does not exist")
            sys.exit(1)
        return p
    # Default: most recent timestamp folder under exports/
    if not EXPORTS_ROOT.exists():
        print("ERROR: exports/ folder doesn't exist")
        sys.exit(1)
    candidates = sorted(
        [d for d in EXPORTS_ROOT.iterdir() if d.is_dir() and (d / "calls" / "audio").exists()],
        key=lambda d: d.name,
        reverse=True,
    )
    if not candidates:
        print("ERROR: no export folders with calls/audio/ found")
        sys.exit(1)
    return candidates[0]


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    export_dir = find_export_folder(arg)
    audio_dir = export_dir / "calls" / "audio"
    calls_file = export_dir / "calls" / "all_calls.json"

    print(f"Export folder: {export_dir}")
    print(f"Audio folder:  {audio_dir}")
    print(f"Calls index:   {calls_file}")
    print()

    if not calls_file.exists():
        print(f"ERROR: {calls_file} not found")
        sys.exit(1)

    with calls_file.open(encoding="utf-8") as f:
        data = json.load(f)

    renamed = 0
    skipped_already = 0
    skipped_missing = 0
    errors = []

    for call in data.get("calls", []):
        url = call.get("recording_url")
        started = call.get("startedAt")
        if not url or not started:
            continue

        current_name = buggy_filename(url)
        if not current_name.endswith(".bin"):
            continue  # not a .bin file, nothing to rename here

        current_path = audio_dir / current_name
        if not current_path.exists():
            # Already renamed in a previous run? Check for any file matching the
            # base name without .bin extension.
            base = current_name[:-4]  # strip .bin
            matches = list(audio_dir.glob(f"*{base}*"))
            if matches:
                skipped_already += 1
            else:
                skipped_missing += 1
            continue

        # Convert UTC startedAt to hotel-local time
        try:
            dt_local = utc_to_local(started, call.get("startedAtFormatted"))
            dt_prefix = dt_local.strftime("%Y-%m-%dT%H-%M-%S")
        except Exception as e:
            errors.append((current_name, f"date parse error: {e}"))
            continue

        correct_ext = correct_extension_from_url(url)
        base = current_name[:-4]  # strip .bin
        new_name = f"{dt_prefix}_{base}{correct_ext}"
        new_path = audio_dir / new_name

        if new_path.exists():
            skipped_already += 1
            continue

        try:
            current_path.rename(new_path)
            renamed += 1
        except Exception as e:
            errors.append((current_name, str(e)))

    print(f"Renamed:                {renamed}")
    print(f"Already renamed:        {skipped_already}")
    print(f"Source file missing:    {skipped_missing}")
    print(f"Errors:                 {len(errors)}")
    if errors:
        print()
        for name, err in errors[:10]:
            print(f"  {name}: {err}")


if __name__ == "__main__":
    main()
