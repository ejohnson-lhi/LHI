#!/usr/bin/env bash
# Idempotent setup for the diarize venv on the droplet.
#
# Usage:
#   ./setup.sh
#
# After this finishes, you still need to:
#   1. huggingface-cli login   (paste a Read token from HF settings)
#   2. Accept the model licenses on these pages:
#        https://huggingface.co/pyannote/speaker-diarization-3.1
#        https://huggingface.co/pyannote/embedding
#   3. huggingface-cli download pyannote/speaker-diarization-3.1
#      huggingface-cli download pyannote/embedding
#
# See README.md for the full procedure.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# Sanity-check system dependencies. PyAV (transitively pulled in by
# faster-whisper) builds from source on Python 3.12 because the
# pinned av==11.* has no prebuilt wheel; the build needs pkg-config
# and ffmpeg dev headers. Fail fast with a clear message instead of
# the cryptic "Getting requirements to build wheel did not run
# successfully" deep inside pip.
if ! command -v pkg-config >/dev/null 2>&1; then
    echo "ERROR: pkg-config not installed. Run:" >&2
    echo "  sudo apt install -y pkg-config ffmpeg libavformat-dev libavcodec-dev libavdevice-dev libavutil-dev libswresample-dev libswscale-dev libavfilter-dev" >&2
    exit 1
fi
if ! pkg-config --exists libavformat 2>/dev/null; then
    echo "ERROR: ffmpeg dev headers not installed. Run:" >&2
    echo "  sudo apt install -y pkg-config ffmpeg libavformat-dev libavcodec-dev libavdevice-dev libavutil-dev libswresample-dev libswscale-dev libavfilter-dev" >&2
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "Creating venv ..."
    python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "Upgrading pip ..."
python -m pip install -U pip wheel

echo "Installing requirements ..."
pip install -r requirements.txt

mkdir -p speaker_profiles

echo
echo "================================================================"
echo "Venv ready at: $HERE/.venv"
echo
echo "Next steps:"
echo "  1. huggingface-cli login"
echo "  2. Accept model licenses on huggingface.co (see README.md)"
echo "  3. huggingface-cli download pyannote/speaker-diarization-3.1"
echo "     huggingface-cli download pyannote/embedding"
echo "  4. Enroll a speaker:"
echo "     python enroll_speaker.py eric ./eric_sample.wav"
echo "  5. Run the batch:"
echo "     python diarize_batch.py"
echo "================================================================"
