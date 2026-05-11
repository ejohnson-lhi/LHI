"""Standalone Kokoro TTS test — synthesizes Iris's greeting straight to a
WAV file, no LiveKit pipeline involved.

Purpose: localize the audio burble. If this WAV plays cleanly but the
in-call recordings have within-word silence gaps, the problem is in the
LiveKit audio publishing path, not in Kokoro itself.

Run on the droplet:
    cd /opt/iris-backend/agent
    .venv/bin/python scripts/kokoro_offline_test.py

Output: /opt/iris-backend/recordings/kokoro_test_<timestamp>.wav
(watch_recordings.bat auto-pulls it to Windows once *.wav is added to the
sync filter — alternatively, scp it down manually.)
"""
from __future__ import annotations

import sys
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
from kokoro_onnx import Kokoro

HERE = Path(__file__).resolve().parent.parent  # .../agent/
MODEL = HERE / "models" / "kokoro-v1.0.onnx"
VOICES = HERE / "models" / "voices-v1.0.bin"
OUT_DIR = Path("/opt/iris-backend/recordings")

TEXT = "Lighthouse Inn, this is Iris, the AI assistant. How may I help you?"
VOICE = "af_sarah"


def main() -> int:
    if not MODEL.exists() or not VOICES.exists():
        print(f"Model files missing under {HERE/'models'}", file=sys.stderr)
        return 1

    print(f"Loading Kokoro from {MODEL.name}...")
    kokoro = Kokoro(model_path=str(MODEL), voices_path=str(VOICES))

    print(f"Synthesizing voice={VOICE}: {TEXT!r}")
    started = datetime.now()
    samples, sr = kokoro.create(TEXT, voice=VOICE, speed=1.0, lang="en-us")
    elapsed = (datetime.now() - started).total_seconds()
    duration = len(samples) / sr
    print(f"Done. {len(samples)} samples @ {sr} Hz "
          f"= {duration:.2f}s of audio, synth took {elapsed:.2f}s "
          f"(rtf={elapsed/duration:.2f})")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"kokoro_test_{started.strftime('%Y%m%d_%H%M%S')}.wav"
    pcm16 = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm16.tobytes())
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
