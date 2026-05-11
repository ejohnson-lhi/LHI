"""Record Iris saying a test script via her actual Vapi/ElevenLabs pipeline.

Workflow:
    # Step 1 — snapshot current config + patch Iris to say the test script.
    python iris_record_test.py prepare

    # Step 2 — call +15419915070 from your cell. Iris says the script.
    #         Hang up after she finishes.

    # Step 3 — download the recording (defaults to most recent call).
    python iris_record_test.py fetch

    # Step 4 — restore the original Iris config.
    python iris_record_test.py restore

Snapshot of pre-test config is saved to iris_snapshot.json next to this script.
Recording goes to ./output/iris_vapi_<callId>.<ext>.

Reads VAPI_API_KEY and VAPI_ASSISTANT_ID from backend/.env via app.config.settings.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

# Allow importing backend.app.config / app.tools.vapi.
HERE = Path(__file__).parent
PROJECT_ROOT = HERE.parent.parent
BACKEND_ROOT = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))
os.chdir(BACKEND_ROOT)  # so dotenv finds backend/.env

import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app.tools import vapi  # noqa: E402

SNAPSHOT_FILE = HERE / "iris_snapshot.json"
OUTPUT_DIR = HERE / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

TEST_SCRIPT = (
    "Thank you for calling Lighthouse Inn in Florence, Oregon. "
    "This is Iris, the front desk assistant. "
    "I can help you check on a reservation, book a new stay, "
    "or answer questions about our property. "
    "Our oceanfront rooms have spectacular views, "
    "complimentary breakfast is served from seven to ten, "
    "and well-behaved pets are welcome with a small fee. "
    "How may I help you today?"
)


async def cmd_prepare() -> None:
    if SNAPSHOT_FILE.exists():
        print(f"Snapshot already exists at {SNAPSHOT_FILE}.")
        print("If a previous test wasn't restored, run 'restore' first; otherwise delete the snapshot file.")
        sys.exit(2)

    print("Fetching current Iris config...")
    current = await vapi.get_assistant()
    if current is None:
        sys.exit("Could not fetch current assistant — check VAPI_API_KEY / VAPI_ASSISTANT_ID")

    snapshot = {
        "firstMessage": current.get("firstMessage"),
        "artifactPlan": current.get("artifactPlan"),
    }
    SNAPSHOT_FILE.write_text(json.dumps(snapshot, indent=2))
    print(f"Snapshot saved to {SNAPSHOT_FILE.name}")
    print(f"  Original firstMessage: {snapshot['firstMessage']!r}")
    print(f"  Original artifactPlan: {snapshot['artifactPlan']}")

    patch = {
        "firstMessage": TEST_SCRIPT,
        "artifactPlan": {"recordingEnabled": True},
    }
    print("\nPatching Iris with test script + recording on...")
    result = await vapi.update_assistant(patch)
    if result is None:
        sys.exit("PATCH failed — see log")
    print("Done. Now call +15419915070 from your cell, listen to Iris read the script, hang up.")
    print("Then run: python iris_record_test.py fetch")


async def cmd_fetch(call_id: str | None = None) -> None:
    """Fetch the recording for the most recent call (or a specific call_id)."""
    base = "https://api.vapi.ai"
    headers = {"Authorization": settings.vapi_api_key}
    aid = settings.vapi_assistant_id

    async with httpx.AsyncClient(timeout=30.0) as client:
        if call_id is None:
            print("Looking up most recent call for this assistant...")
            r = await client.get(
                f"{base}/call",
                headers=headers,
                params={"assistantId": aid, "limit": 5},
            )
            if r.status_code != 200:
                sys.exit(f"Could not list calls: HTTP {r.status_code}: {r.text[:300]}")
            calls = r.json()
            if not calls:
                sys.exit("No recent calls found for this assistant.")
            for i, c in enumerate(calls):
                print(
                    f"  [{i}] {c.get('id')} status={c.get('status')} "
                    f"started={c.get('startedAt')} ended={c.get('endedAt')} "
                    f"recording={'yes' if c.get('recordingUrl') else 'no'}"
                )
            chosen = calls[0]
            call_id = chosen.get("id")
            print(f"Using most recent: {call_id}")

        r = await client.get(f"{base}/call/{call_id}", headers=headers)
        if r.status_code != 200:
            sys.exit(f"Could not fetch call {call_id}: HTTP {r.status_code}: {r.text[:300]}")
        call = r.json()

        rec_url = call.get("recordingUrl") or (call.get("artifact") or {}).get("recordingUrl")
        if not rec_url:
            print(f"No recordingUrl yet on call {call_id}.")
            print(f"Status: {call.get('status')}, ended: {call.get('endedAt')}")
            print("Recordings can take 30–60 seconds to finalize after hangup. Try again shortly.")
            return

        print(f"Recording URL: {rec_url}")
        # File extension from URL
        ext = rec_url.rsplit(".", 1)[-1].split("?", 1)[0] or "wav"
        if len(ext) > 5:
            ext = "mp3"
        out_path = OUTPUT_DIR / f"iris_vapi_{call_id[:8]}.{ext}"

        print(f"Downloading -> {out_path.name}")
        rec_response = await client.get(rec_url)
        if rec_response.status_code != 200:
            sys.exit(f"Recording download failed: HTTP {rec_response.status_code}")
        out_path.write_bytes(rec_response.content)
        print(f"Done. {len(rec_response.content):,} bytes")
        print(f"\nFile: {out_path}")


async def cmd_restore() -> None:
    if not SNAPSHOT_FILE.exists():
        sys.exit(f"No snapshot found at {SNAPSHOT_FILE}. Was prepare ever run?")
    snapshot = json.loads(SNAPSHOT_FILE.read_text())

    patch = {"firstMessage": snapshot.get("firstMessage")}
    if snapshot.get("artifactPlan") is not None:
        patch["artifactPlan"] = snapshot["artifactPlan"]
    else:
        # Original had no artifactPlan; pass an empty object to remove ours.
        # If Vapi rejects this, leaving recordingEnabled on doesn't hurt — Iris
        # will still record going forward, which is probably what we want anyway.
        patch["artifactPlan"] = {}

    print("Restoring original Iris config...")
    result = await vapi.update_assistant(patch)
    if result is None:
        sys.exit("PATCH failed — see log")
    SNAPSHOT_FILE.unlink()
    print(f"Done. firstMessage restored to: {snapshot.get('firstMessage')!r}")
    print(f"Snapshot file removed.")


def usage() -> None:
    print(__doc__)
    sys.exit(2)


async def main() -> None:
    if len(sys.argv) < 2:
        usage()
    cmd = sys.argv[1]
    if cmd == "prepare":
        await cmd_prepare()
    elif cmd == "fetch":
        cid = sys.argv[2] if len(sys.argv) > 2 else None
        await cmd_fetch(cid)
    elif cmd == "restore":
        await cmd_restore()
    else:
        usage()


if __name__ == "__main__":
    asyncio.run(main())
