"""Enroll a speaker's voice fingerprint from a clean audio sample.

Usage:
    python enroll_speaker.py <name> <audio_file>

Writes speaker_profiles/<name>.npy — a 192-dim pyannote embedding (the
"engram"). diarize_batch.py compares each call's anonymous speaker
segments against these enrolled profiles via cosine similarity to
assign matched names.

Notes:
- 30-60 seconds of clean speech is plenty. Background noise is OK but
  cross-talk (two people at once) confuses the embedding.
- Re-run with the same name to overwrite the profile. Useful if the
  original sample turns out to have poor match quality.
- Profiles are gitignored — never commit voice fingerprints.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import torch

# pyannote.audio's lazy imports are slow, but only Inference is needed.
from pyannote.audio import Model, Inference

HERE = Path(__file__).parent
PROFILES_DIR = HERE / "speaker_profiles"
HF_TOKEN = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")


def enroll(name: str, audio_path: Path) -> Path:
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    # `pyannote/embedding` is a 192-dim ECAPA-TDNN-style speaker embedder.
    # `window="whole"` averages the embedding over the whole clip — best
    # for enrollment where we want a single representative vector.
    print(f"Loading pyannote/embedding model ...")
    model = Model.from_pretrained(
        "pyannote/embedding",
        use_auth_token=HF_TOKEN,
    )
    # Send to CPU explicitly. (Override to "cuda" if GPU is available.)
    device = torch.device("cpu")
    inference = Inference(model, window="whole", device=device)

    print(f"Computing embedding from {audio_path.name} ...")
    embedding = inference(str(audio_path))
    # pyannote returns a numpy array shape (1, 192) or (192,) depending
    # on version. Normalize to flat 1-D so cosine math is symmetric with
    # the runtime path in diarize_batch.py.
    arr = np.asarray(embedding).reshape(-1)
    if arr.size != 192:
        print(
            f"WARNING: expected 192-dim embedding, got {arr.size}-dim. "
            f"Profile saved but match logic may not work as expected.",
            file=sys.stderr,
        )

    out = PROFILES_DIR / f"{name}.npy"
    np.save(out, arr)
    norm = float(np.linalg.norm(arr))
    print(f"Enrolled {name!r} → {out}")
    print(f"  Dim: {arr.size}")
    print(f"  Norm: {norm:.3f}  (typical ~1.0 after pyannote normalization)")
    return out


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    name = sys.argv[1].strip().lower()
    if not name.isidentifier():
        print(
            f"Speaker name must be a valid identifier (letters/digits/underscore). Got {name!r}.",
            file=sys.stderr,
        )
        return 2
    audio_path = Path(sys.argv[2]).expanduser().resolve()
    enroll(name, audio_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
