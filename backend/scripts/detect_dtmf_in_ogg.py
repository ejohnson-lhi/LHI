"""Scan an OGG/Opus recording for DTMF tone energy. Answers the question:
'Did the egress recording capture DTMF tones from the call?' If yes, the
hands-free in-call card-capture path needs an extra audio-track-mute step
during capture; if no, the Twilio RFC 4733 path is delivering DTMF as
out-of-band events only and the recording is naturally clean.

Method: Goertzel filter at each of the 8 DTMF frequencies (697/770/852/941
Hz × 1209/1336/1477/1633 Hz), 40ms windows with 20ms hop. A detection
requires (a) one row frequency dominant within row band, (b) one column
frequency dominant within column band, (c) both row and column powers
substantially above the off-band noise floor — i.e., a clear cross-pair
above whatever speech happens to be present.

Run:
    .venv\\Scripts\\python scripts\\detect_dtmf_in_ogg.py <path-to-ogg>
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

# DTMF frequency grid
ROW_FREQS = (697.0, 770.0, 852.0, 941.0)
COL_FREQS = (1209.0, 1336.0, 1477.0, 1633.0)
DIGIT_GRID = {
    (697, 1209): "1", (697, 1336): "2", (697, 1477): "3", (697, 1633): "A",
    (770, 1209): "4", (770, 1336): "5", (770, 1477): "6", (770, 1633): "B",
    (852, 1209): "7", (852, 1336): "8", (852, 1477): "9", (852, 1633): "C",
    (941, 1209): "*", (941, 1336): "0", (941, 1477): "#", (941, 1633): "D",
}

WINDOW_MS = 40
HOP_MS = 20
# Dominance ratio: top row power must be >= 3× second-best row power
DOMINANCE_RATIO = 3.0
# Cross-pair power floor: each of the winning row+col must contribute
# at least this fraction of the window's total energy
ENERGY_FRACTION_MIN = 0.05


def goertzel_power(samples: np.ndarray, freq: float, sr: int) -> float:
    """Power at `freq` in `samples`. samples assumed to be a 1-D float
    window. Returns normalized magnitude squared."""
    n = len(samples)
    if n == 0:
        return 0.0
    coeff = 2.0 * math.cos(2.0 * math.pi * freq / sr)
    s_prev = 0.0
    s_prev2 = 0.0
    for x in samples:
        s = float(x) + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    p = s_prev * s_prev + s_prev2 * s_prev2 - coeff * s_prev * s_prev2
    return max(p, 0.0) / (n * n)


def scan_window(window: np.ndarray, sr: int) -> tuple[str, float, float] | None:
    """Return (digit, row_power_ratio, col_power_ratio) if a DTMF cross-pair
    is detected in this window; else None."""
    if window.size == 0:
        return None
    total_energy = float(np.mean(window.astype(np.float64) ** 2))
    if total_energy < 1e-9:
        return None
    row_powers = [goertzel_power(window, f, sr) for f in ROW_FREQS]
    col_powers = [goertzel_power(window, f, sr) for f in COL_FREQS]
    top_row_idx = int(np.argmax(row_powers))
    top_col_idx = int(np.argmax(col_powers))
    top_row = row_powers[top_row_idx]
    top_col = col_powers[top_col_idx]
    other_rows = sorted(row_powers, reverse=True)[1:]
    other_cols = sorted(col_powers, reverse=True)[1:]
    second_row = other_rows[0] if other_rows else 0.0
    second_col = other_cols[0] if other_cols else 0.0
    if second_row > 0 and top_row / second_row < DOMINANCE_RATIO:
        return None
    if second_col > 0 and top_col / second_col < DOMINANCE_RATIO:
        return None
    if top_row / total_energy < ENERGY_FRACTION_MIN:
        return None
    if top_col / total_energy < ENERGY_FRACTION_MIN:
        return None
    digit = DIGIT_GRID.get((int(ROW_FREQS[top_row_idx]), int(COL_FREQS[top_col_idx])))
    if digit is None:
        return None
    return digit, top_row / total_energy, top_col / total_energy


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: detect_dtmf_in_ogg.py <path-to-ogg>")
        return 2
    path = Path(argv[1]).expanduser()
    if not path.exists():
        print(f"NOT FOUND: {path}")
        return 2

    print(f"Loading: {path.name}")
    data, sr = sf.read(str(path), always_2d=False, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    duration_s = len(data) / sr
    print(f"  sample_rate={sr} Hz, duration={duration_s:.2f}s, samples={len(data)}")

    window_n = int(sr * WINDOW_MS / 1000)
    hop_n = int(sr * HOP_MS / 1000)
    n_windows = max(0, 1 + (len(data) - window_n) // hop_n)
    print(f"  scanning {n_windows} windows of {WINDOW_MS}ms (hop {HOP_MS}ms) ...")

    detections: list[tuple[float, str, float, float]] = []
    for w in range(n_windows):
        start = w * hop_n
        end = start + window_n
        det = scan_window(data[start:end], sr)
        if det is not None:
            digit, r_ratio, c_ratio = det
            t = start / sr
            detections.append((t, digit, r_ratio, c_ratio))

    print()
    if not detections:
        print("RESULT: NO DTMF tones detected in THIS OGG.")
        print("  Could mean: (a) caller did not press any keys, (b) this is")
        print("  the agent-TTS leg with no caller audio, or (c) the audio is")
        print("  genuinely clean of in-band tones. Compare against the other")
        print("  per-participant OGG from the same call to disambiguate.")
        return 0

    # Collapse adjacent detections of the same digit into one event each
    collapsed: list[tuple[float, str, int]] = []
    for t, digit, _r, _c in detections:
        if collapsed and collapsed[-1][1] == digit and t - collapsed[-1][0] < 0.2:
            collapsed[-1] = (collapsed[-1][0], digit, collapsed[-1][2] + 1)
        else:
            collapsed.append((t, digit, 1))

    print(f"RESULT: DTMF DETECTED — {len(collapsed)} discrete press(es), "
          f"{len(detections)} raw windows above threshold.")
    print("  This means tones are IN the audio that egress is recording.")
    print("  The card-capture path needs to mute the caller's published audio")
    print("  track during the capture window, or we leak digits into the OGG.")
    print()
    print("  Detected presses (time -> digit, contiguous windows):")
    for t, digit, n in collapsed[:50]:
        print(f"    t={t:7.3f}s  digit={digit!r}  windows={n}")
    if len(collapsed) > 50:
        print(f"    ... and {len(collapsed) - 50} more.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
