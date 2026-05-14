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
