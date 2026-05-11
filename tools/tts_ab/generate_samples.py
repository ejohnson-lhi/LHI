"""TTS A/B comparison: Kokoro (open-source) vs ElevenLabs vs Cartesia (commercial).

Generates the same hotel-front-desk text in multiple voices from each engine
and saves WAV/MP3 files to ./output/ for listening.

Setup (already done if you ran the install commands):
    python -m venv .venv
    .venv\\Scripts\\activate
    pip install kokoro-onnx soundfile httpx

Required files in this directory:
    kokoro-v1.0.onnx   (~325 MB)
    voices-v1.0.bin    (~36 MB)

For ElevenLabs samples (Iris's current voice), set ELEVENLABS_API_KEY env var.
For Cartesia samples, set CARTESIA_API_KEY env var.
"""
import os
import sys
from pathlib import Path

import httpx
import soundfile as sf
from kokoro_onnx import Kokoro

HERE = Path(__file__).parent
OUTPUT = HERE / "output"
OUTPUT.mkdir(exist_ok=True)

# ~15 seconds of speech in a typical hotel front-desk register.
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

# Kokoro v1.0 voices (American English).
# Naming convention: af_* = American Female, am_* = American Male
KOKORO_VOICES = [
    "af_bella",      # warm, popular voice
    "af_sarah",      # neutral, professional
    "af_nicole",     # softer
    "am_michael",    # male professional
    "am_adam",       # male warm
]

# Cartesia voice IDs (Sonic-2 multilingual).
# These are public voice IDs from Cartesia's library — check the latest at
# https://docs.cartesia.ai/build-with-cartesia/capability-guides/specify-a-voice
CARTESIA_VOICES = {
    "professional_woman": "f9836c6e-a0bd-460e-9d3c-f7299fa60f94",  # Sophie / professional
    "warm_female": "248be419-c632-4f23-adf1-5324ed7dbf1d",          # Hannah
    "calm_lady": "00a77add-48d5-4ef6-8157-71e5437b282d",            # Calm Lady
}

# ElevenLabs — Iris's actual current voice config (from sync_to_vapi.py).
ELEVENLABS_VOICES = {
    "cherie_r_iris_current": "vr5WKaGvRWsoaX5LCVax",  # the voice Iris uses today
}
ELEVENLABS_MODEL = "eleven_turbo_v2_5"


def generate_kokoro() -> None:
    print("Loading Kokoro model...")
    kokoro = Kokoro(
        model_path=str(HERE / "kokoro-v1.0.onnx"),
        voices_path=str(HERE / "voices-v1.0.bin"),
    )
    print(f"Kokoro voices available: {len(kokoro.get_voices())} total")

    for voice in KOKORO_VOICES:
        print(f"  Generating Kokoro/{voice}...")
        try:
            samples, sample_rate = kokoro.create(SAMPLE_TEXT, voice=voice, speed=1.0, lang="en-us")
            output_path = OUTPUT / f"kokoro_{voice}.wav"
            sf.write(output_path, samples, sample_rate)
            duration_s = len(samples) / sample_rate
            print(f"    -> {output_path.name} ({duration_s:.1f}s)")
        except Exception as e:
            print(f"    FAILED: {e}")


def generate_elevenlabs(api_key: str) -> None:
    print("\nGenerating ElevenLabs samples...")
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
    }

    for label, voice_id in ELEVENLABS_VOICES.items():
        print(f"  Generating ElevenLabs/{label}...")
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128"
        payload = {
            "text": SAMPLE_TEXT,
            "model_id": ELEVENLABS_MODEL,
        }
        try:
            response = httpx.post(url, headers=headers, json=payload, timeout=60.0)
            if response.status_code != 200:
                print(f"    FAILED: HTTP {response.status_code}: {response.text[:300]}")
                continue
            output_path = OUTPUT / f"elevenlabs_{label}.mp3"
            output_path.write_bytes(response.content)
            print(f"    -> {output_path.name} ({len(response.content)} bytes)")
        except Exception as e:
            print(f"    FAILED: {e}")


def generate_cartesia(api_key: str) -> None:
    print("\nGenerating Cartesia samples...")
    url = "https://api.cartesia.ai/tts/bytes"
    headers = {
        "Cartesia-Version": "2024-06-10",
        "X-API-Key": api_key,
        "Content-Type": "application/json",
    }

    for label, voice_id in CARTESIA_VOICES.items():
        print(f"  Generating Cartesia/{label}...")
        payload = {
            "model_id": "sonic-2",
            "transcript": SAMPLE_TEXT,
            "voice": {"mode": "id", "id": voice_id},
            "output_format": {
                "container": "wav",
                "encoding": "pcm_s16le",
                "sample_rate": 24000,
            },
            "language": "en",
        }
        try:
            response = httpx.post(url, headers=headers, json=payload, timeout=60.0)
            if response.status_code != 200:
                print(f"    FAILED: HTTP {response.status_code}: {response.text[:200]}")
                continue
            output_path = OUTPUT / f"cartesia_{label}.wav"
            output_path.write_bytes(response.content)
            print(f"    -> {output_path.name} ({len(response.content)} bytes)")
        except Exception as e:
            print(f"    FAILED: {e}")


if __name__ == "__main__":
    if not (HERE / "kokoro-v1.0.onnx").exists():
        sys.exit(f"Missing model file at {HERE / 'kokoro-v1.0.onnx'}")
    if not (HERE / "voices-v1.0.bin").exists():
        sys.exit(f"Missing voices file at {HERE / 'voices-v1.0.bin'}")

    # Allow skipping Kokoro on re-runs (it's slow) by setting SKIP_KOKORO=1
    if not os.environ.get("SKIP_KOKORO"):
        generate_kokoro()
    else:
        print("Skipping Kokoro (SKIP_KOKORO env var set).")

    elevenlabs_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if elevenlabs_key:
        generate_elevenlabs(elevenlabs_key)
    else:
        print("\nSkipping ElevenLabs samples — ELEVENLABS_API_KEY env var not set.")
        print("To get one: sign up at https://elevenlabs.io/app/sign-up (free tier: 10K chars/month),")
        print("create an API key in Profile → API Keys, then re-run with:")
        print("    set ELEVENLABS_API_KEY=<your-key>")

    cartesia_key = os.environ.get("CARTESIA_API_KEY", "").strip()
    if cartesia_key:
        generate_cartesia(cartesia_key)
    else:
        print("\nSkipping Cartesia samples — CARTESIA_API_KEY env var not set.")
        print("To get one: sign up free at https://play.cartesia.ai/sign-in (free trial credits),")
        print("then re-run with: set CARTESIA_API_KEY=<your-key>")

    print(f"\nDone. Sample files in: {OUTPUT}")
