"""Generate any Kokoro voices not yet present in ./output/.

Idempotent: skips voices whose output WAV already exists. Re-run any time to
add new voices to the list below or to fill gaps from a partial run.

Usage:
    .venv\\Scripts\\python.exe generate_more_kokoro.py
"""
from pathlib import Path

import soundfile as sf
from kokoro_onnx import Kokoro

HERE = Path(__file__).parent
OUTPUT = HERE / "output"
OUTPUT.mkdir(exist_ok=True)

SAMPLE_TEXT = (
    "Thank you for calling Lighthouse Inn in Florence, Oregon. "
    "This is Iris, the front desk assistant. "
    "I can help you check on a reservation, book a new stay, "
    "or answer questions about our property. "
    "Our oceanfront rooms have spectacular views, "
    "complimentary breakfast is served from seven to ten, "
    "and well-behaved pets are welcome with a small fee. "
    "How may I help you today?"
)

# All American English voices in Kokoro v1.0.
AMERICAN_VOICES = [
    # Female
    "af_alloy", "af_aoede", "af_bella", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    # Male
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam",
    "am_michael", "am_onyx", "am_puck", "am_santa",
]


def main() -> None:
    print("Loading Kokoro model...")
    kokoro = Kokoro(
        model_path=str(HERE / "kokoro-v1.0.onnx"),
        voices_path=str(HERE / "voices-v1.0.bin"),
    )

    available = set(kokoro.get_voices())
    generated = 0
    skipped_existing = 0
    skipped_missing = 0

    for voice in AMERICAN_VOICES:
        out = OUTPUT / f"kokoro_{voice}.wav"
        if out.exists():
            print(f"  [skip] kokoro_{voice}.wav already exists")
            skipped_existing += 1
            continue
        if voice not in available:
            print(f"  [skip] {voice} not in model voice set")
            skipped_missing += 1
            continue

        print(f"  Generating {voice}...")
        try:
            samples, sample_rate = kokoro.create(
                SAMPLE_TEXT, voice=voice, speed=1.0, lang="en-us"
            )
            sf.write(out, samples, sample_rate)
            duration_s = len(samples) / sample_rate
            print(f"    -> {out.name} ({duration_s:.1f}s)")
            generated += 1
        except Exception as e:
            print(f"    FAILED: {e}")

    print(
        f"\nDone. {generated} new, {skipped_existing} already existed, "
        f"{skipped_missing} not in model."
    )
    print(f"Output: {OUTPUT}")


if __name__ == "__main__":
    main()
