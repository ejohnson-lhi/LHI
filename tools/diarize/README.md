# Diarization batch transcription

Self-hosted nightly job: transcribe new call recordings with **speaker diarization** (who-spoke-when) and **speaker fingerprinting** (which-known-person), emit enriched JSON for downstream mining.

The whole pipeline runs on the droplet. No external API calls, no per-minute fees, no audio leaves the box.

## Why

Iris's existing JSON transcripts (in `recordings/transcript_*.json`) already tag user vs assistant for AI-engaged calls. They do *not* help for:

- **Silent-mode calls** on the production DID (+15419915070): Iris stays muted while the caller talks to a human at the front desk. We have OGG recordings of those conversations, but no transcript and no idea which voice belongs to whom.
- **Authority weighting** when curating prompt + intent-cache responses: the owner (Eric) gives definitive answers; family / employees / part-time staff may answer the same question differently. We want Eric's wording to weigh more in derived prompts than other people's.

This job solves both by producing per-segment transcripts tagged with a matched speaker name.

## Pipeline

```
OGG ─► WhisperX (Whisper + pyannote)
        │
        ├── transcribed segments with timestamps
        └── diarization labels  (anonymous SPEAKER_00, _01, _02)
                │
                ▼
        pyannote speaker embedding per anonymous speaker
                │
                ▼
        cosine-similarity match to enrolled fingerprints
                (speaker_profiles/*.npy)
                │
                ▼
        per-segment matched name: "eric" | "unknown" | etc.
                │
                ▼
        Enriched JSON: recordings/transcribed/<basename>.json
```

## One-time setup (on the droplet)

```bash
cd /opt/iris-backend/tools/diarize

# Before anything else: open these two URLs in a browser (logged into HF)
# and click "Agree and access repository" — the model downloads 401
# without this:
#   https://huggingface.co/pyannote/speaker-diarization-3.1
#   https://huggingface.co/pyannote/embedding

# Run the setup script. Creates .venv (separate from the agent's runtime
# venv — heavy deps like PyTorch ~2 GB), installs requirements.
bash setup.sh

# Critical: activate the venv so huggingface-cli is on PATH:
source .venv/bin/activate

# Paste a Read token from https://huggingface.co/settings/tokens
huggingface-cli login

# Pre-fetch the models so the first run isn't slow:
huggingface-cli download pyannote/speaker-diarization-3.1
huggingface-cli download pyannote/embedding
```

## Enroll a speaker (one-time per known person)

Record a clean 30-60 second audio sample. Phone Voice Memos or any recording app works. Drop the file on the droplet (scp / sftp / etc.) and:

```bash
source /opt/iris-backend/tools/diarize/.venv/bin/activate
python /opt/iris-backend/tools/diarize/enroll_speaker.py eric ./eric_sample.wav
```

Writes `speaker_profiles/eric.npy` (a 192-dim numpy array — the voice fingerprint). The profiles directory is gitignored so fingerprints stay on the droplet.

Repeat for any other speakers you want to identify (e.g. `wife`, `frontdesk_jane`). Unknown speakers stay as `unknown` in the output, still consistently grouped per call.

## Batch run

```bash
source /opt/iris-backend/tools/diarize/.venv/bin/activate
python /opt/iris-backend/tools/diarize/diarize_batch.py
```

Processes every `recordings/iris-call-*.ogg` that doesn't already have a `recordings/transcribed/<basename>.json`. Skips OGGs that already have a sibling transcript. Idempotent — safe to re-run.

Performance on the droplet (CPU-only, no GPU): roughly 5-10× realtime, so a 4-minute call takes 20-40 seconds. Whisper-large-v3 with int8 quantization.

## Nightly cron (on droplet)

```cron
# /etc/cron.d/iris-diarize
0 2 * * * iris /opt/iris-backend/tools/diarize/.venv/bin/python /opt/iris-backend/tools/diarize/diarize_batch.py >> /var/log/iris-diarize.log 2>&1
```

## Output format

`recordings/transcribed/<basename>.json`:

```json
{
  "source_ogg": "iris-call-_+15419915070_xyz-frontdesk2-TR_AM...-2026-05-14T193105.ogg",
  "processed_at": "2026-05-15T02:00:14...",
  "duration_seconds": 247.3,
  "speakers_present": {
    "SPEAKER_00": "eric",
    "SPEAKER_01": "unknown",
    "SPEAKER_02": "wife"
  },
  "segments": [
    {
      "start": 0.0,
      "end": 2.3,
      "text": "Lighthouse Inn, this is Eric.",
      "speaker": "SPEAKER_00",
      "matched_name": "eric",
      "match_score": 0.81
    },
    {
      "start": 2.5,
      "end": 5.1,
      "text": "Hi, do you allow dogs?",
      "speaker": "SPEAKER_01",
      "matched_name": "unknown",
      "match_score": 0.32
    }
  ]
}
```

Downstream: `tools/mine_intents.py` reads this format and applies speaker weighting when curating intent_cache responses (Eric's answers get higher weight than unknown speakers').

## File-naming context

The egress filename template at `deploy/livekit/config/dispatch-rule.json.example` includes `{publisher_identity}` so each per-track OGG carries the leg identity. To apply the updated rule:

```bash
cd /opt/iris-backend

# See the current rule and note its ID:
lk sip dispatch-rule list

# Copy + fill in the real ST_xxx trunk ID:
cp deploy/livekit/config/dispatch-rule.json.example deploy/livekit/config/dispatch-rule.json
# edit deploy/livekit/config/dispatch-rule.json — replace ST_REPLACE_WITH_REAL_TRUNK_ID
# (dispatch-rule.json is gitignored — the trunk ID stays out of the repo)

# livekit-cli does not have an in-place update, so delete + recreate:
lk sip dispatch-rule delete <rule-id-from-list>
lk sip dispatch-rule create deploy/livekit/config/dispatch-rule.json
```

After that, new calls produce filenames like:

- `iris-call-..._<caller_phone>-TR_...ogg` — caller leg
- `iris-call-..._frontdesk2-TR_...ogg` — hotel front-desk leg (silent-mode 5070 only)
- `iris-call-..._agent-<random>-TR_...ogg` — Iris's leg (silent in 5070, speaking in 5071)

For pre-fix OGGs without `{publisher_identity}` in the name, the batch still diarizes them; you just have to look at the transcript content to figure out which leg is which.
