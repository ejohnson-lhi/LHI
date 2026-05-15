"""Nightly batch: transcribe new call recordings + match the single speaker
in each per-leg OGG to enrolled fingerprints, write enriched JSON.

KEY ASSUMPTION: LiveKit's per-track egress (config: `egress.tracks` in the
dispatch rule) produces ONE OGG per audio participant. So each OGG contains
audio from exactly one speaker — Iris's TTS leg, OR the caller's mic leg,
OR the transferred front-desk leg. We do NOT run pyannote diarization;
running it on single-speaker audio just produces false-positive speaker
splits (we saw 4 anonymous speakers in a 2-minute Iris-only OGG).

Pipeline:
  1. faster-whisper (large-v3 int8) + VAD → segments with timestamps/text
  2. pyannote.audio embedding over the speaking portions of the file → one
     192- or 512-dim speaker fingerprint for the whole file
  3. Cosine-match the fingerprint against enrolled speaker_profiles/*.npy
  4. Emit JSON: top-level matched_name + match_score + transcription segments

Idempotent: scans recordings/ for OGGs without a sibling
recordings/transcribed/<basename>.json, processes only the new ones.

Run manually:
    source /opt/iris-backend/tools/diarize/.venv/bin/activate
    python diarize_batch.py

Or via cron (see README.md).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf

log = logging.getLogger("diarize_batch")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

HERE = Path(__file__).parent
PROJECT_ROOT = HERE.parent.parent
RECORDINGS_DIR = Path(
    os.environ.get("IRIS_RECORDINGS_DIR", PROJECT_ROOT / "recordings")
)
OUTPUT_DIR = RECORDINGS_DIR / "transcribed"
PROFILES_DIR = HERE / "speaker_profiles"

HF_TOKEN = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
MATCH_THRESHOLD = float(os.environ.get("MATCH_THRESHOLD", "0.55"))
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "large-v3")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8")
TARGET_SR = 16000  # pyannote and Whisper both want 16 kHz mono

# Minimum amount of speech (after VAD) needed to compute a reliable
# embedding. Below this, the file is mostly silence (e.g. Iris's leg
# during a call where she only spoke at the start) and we skip the match.
MIN_SPEECH_SECONDS = 1.0


# =============================================================================
# Speaker profile loading + cosine matching
# =============================================================================

def load_profiles() -> dict[str, np.ndarray]:
    """Read every speaker_profiles/<name>.npy. Returns {name: embedding}."""
    if not PROFILES_DIR.exists():
        log.warning(
            "No %s directory; running without speaker fingerprinting. "
            "All files will be labelled 'unknown'. Run enroll_speaker.py first.",
            PROFILES_DIR,
        )
        return {}
    profiles: dict[str, np.ndarray] = {}
    for f in sorted(PROFILES_DIR.glob("*.npy")):
        try:
            arr = np.load(f).reshape(-1)
            profiles[f.stem] = arr
            log.info("Loaded profile %r (dim=%d, norm=%.3f)",
                     f.stem, arr.size, float(np.linalg.norm(arr)))
        except Exception as e:
            log.warning("Could not load profile %s: %s", f, e)
    if not profiles:
        log.warning("speaker_profiles/ exists but is empty — no known speakers to match.")
    return profiles


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity in [-1, 1]. Both inputs treated as 1-D."""
    a = a.reshape(-1).astype(np.float32)
    b = b.reshape(-1).astype(np.float32)
    if a.shape != b.shape:
        return 0.0
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def best_match(
    embedding: np.ndarray,
    profiles: dict[str, np.ndarray],
    threshold: float = MATCH_THRESHOLD,
) -> tuple[str, float, dict[str, float]]:
    """Return (matched_name, top_score, all_scores).
    `matched_name` is 'unknown' if top_score < threshold.
    `all_scores` is the full {profile_name: score} map for diagnostics."""
    if not profiles:
        return ("unknown", 0.0, {})
    all_scores = {name: cosine(embedding, prof) for name, prof in profiles.items()}
    sorted_pairs = sorted(all_scores.items(), key=lambda t: t[1], reverse=True)
    top_name, top_score = sorted_pairs[0]
    if top_score >= threshold:
        return (top_name, top_score, all_scores)
    return ("unknown", top_score, all_scores)


# =============================================================================
# Per-OGG processing
# =============================================================================

def process_one(
    ogg_path: Path,
    profiles: dict[str, np.ndarray],
    asr_model,
    embedding_inference,
) -> dict:
    """Process a single per-leg OGG. Single speaker assumed; no diarization."""
    import librosa

    log.info("Loading audio: %s", ogg_path.name)
    audio, sr = librosa.load(str(ogg_path), sr=TARGET_SR, mono=True)
    duration = len(audio) / sr

    # ---- 1. Transcribe with faster-whisper (built-in VAD filter) ---------
    log.info("  Transcribing (Whisper %s, %s/%s)...",
             WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE)
    fw_segments_iter, info = asr_model.transcribe(
        str(ogg_path),
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )
    fw_segments = list(fw_segments_iter)
    language = getattr(info, "language", "en")

    # ---- 2. Compute ONE embedding for the speaking portions of the file --
    matched_name: str = "unknown"
    match_score: float = 0.0
    all_scores: dict[str, float] = {}
    embedding_dim: int = 0
    speech_seconds: float = 0.0

    if fw_segments:
        # Build the audio of only the speech windows (VAD already chose
        # these). Concatenating keeps the embedding focused on the speaker
        # and away from silence.
        speech_chunks: list[np.ndarray] = []
        for fs in fw_segments:
            s_idx = max(0, int(fs.start * sr))
            e_idx = min(len(audio), int(fs.end * sr))
            if e_idx > s_idx:
                speech_chunks.append(audio[s_idx:e_idx])
        if speech_chunks:
            speech_audio = np.concatenate(speech_chunks)
            speech_seconds = len(speech_audio) / sr

            if profiles and speech_seconds >= MIN_SPEECH_SECONDS:
                log.info("  Computing speaker embedding from %.1fs of speech ...",
                         speech_seconds)
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    sf.write(tmp.name, speech_audio, sr)
                    tmp_path = tmp.name
                try:
                    emb = embedding_inference(tmp_path)
                    emb = np.asarray(emb).reshape(-1)
                    embedding_dim = int(emb.size)
                    matched_name, match_score, all_scores = best_match(
                        emb, profiles,
                    )
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            elif not profiles:
                log.info("  No enrolled profiles — skipping match")
            else:
                log.info(
                    "  Only %.2fs of speech (< %.1fs threshold) — skipping match",
                    speech_seconds, MIN_SPEECH_SECONDS,
                )

    # ---- 3. Build output JSON ---------------------------------------------
    segments = [
        {
            "start": round(fs.start, 2),
            "end": round(fs.end, 2),
            "text": (fs.text or "").strip(),
        }
        for fs in fw_segments
    ]

    return {
        "source_ogg": ogg_path.name,
        "processed_at": datetime.now().isoformat(),
        "duration_seconds": round(duration, 2),
        "speech_seconds": round(speech_seconds, 2),
        "language": language,
        "match_threshold": MATCH_THRESHOLD,
        "matched_name": matched_name,
        "match_score": round(match_score, 3),
        "embedding_dim": embedding_dim,
        "all_scores": {k: round(v, 3) for k, v in all_scores.items()},
        "segments": segments,
    }


# =============================================================================
# Driver
# =============================================================================

def find_pending_oggs() -> list[Path]:
    """Every OGG in RECORDINGS_DIR without a sibling JSON in transcribed/."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    done_stems = {p.stem for p in OUTPUT_DIR.glob("*.json")}
    pending = [
        p for p in sorted(RECORDINGS_DIR.glob("iris-call-*.ogg"))
        if p.stem not in done_stems
    ]
    return pending


def main() -> int:
    pending = find_pending_oggs()
    if not pending:
        log.info("Nothing to do — no untranscribed OGGs in %s", RECORDINGS_DIR)
        return 0
    log.info("%d OGG(s) pending transcription", len(pending))

    profiles = load_profiles()

    # Heavy imports + model loads happen once for the whole batch.
    from faster_whisper import WhisperModel
    from pyannote.audio import Model, Inference
    import torch

    log.info("Loading faster-whisper model: %s (%s/%s) ...",
             WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE)
    asr_model = WhisperModel(
        WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE,
    )

    log.info("Loading pyannote speaker embedding model ...")
    try:
        emb_model = Model.from_pretrained(
            "pyannote/embedding", token=HF_TOKEN,
        )
    except TypeError:
        emb_model = Model.from_pretrained(
            "pyannote/embedding", use_auth_token=HF_TOKEN,
        )
    embedding_inference = Inference(
        emb_model, window="whole",
        device=torch.device(WHISPER_DEVICE),
    )

    succeeded = failed = 0
    for ogg in pending:
        try:
            t0 = datetime.now()
            enriched = process_one(
                ogg, profiles, asr_model, embedding_inference,
            )
            out_json = OUTPUT_DIR / f"{ogg.stem}.json"
            out_json.write_text(
                json.dumps(enriched, indent=2, default=str),
                encoding="utf-8",
            )
            elapsed = (datetime.now() - t0).total_seconds()
            log.info(
                "  -> %s  (%.1fs wall, matched=%s score=%.3f, %d segments)",
                out_json.name, elapsed,
                enriched.get("matched_name", "?"),
                enriched.get("match_score", 0.0),
                len(enriched.get("segments", [])),
            )
            succeeded += 1
        except Exception as e:
            log.exception("FAILED on %s: %s", ogg.name, e)
            failed += 1

    log.info("Done. Succeeded=%d Failed=%d", succeeded, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
