"""Nightly batch: diarize + transcribe new call recordings, attach
speaker fingerprint matches, write enriched JSON.

Idempotent: scans recordings/ for OGGs without a sibling
recordings/transcribed/<basename>.json, processes only the new ones.

Run manually:
    source /opt/iris-backend/tools/diarize/.venv/bin/activate
    python diarize_batch.py

Or via cron (see README.md).

Pipeline:
  1. WhisperX large-v3 (int8) → segments with timestamps
  2. Word-level alignment (wav2vec2)
  3. pyannote 3.1 diarization → speaker labels per segment
  4. pyannote embedding per anonymous speaker → match to
     speaker_profiles/*.npy by cosine similarity
  5. Emit enriched JSON with matched_name per segment

Threshold for "known speaker" match defaults to 0.55 (cosine sim).
Below that, the speaker stays "unknown". The threshold can be tuned via
the MATCH_THRESHOLD env var.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

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


# =============================================================================
# Speaker profile loading + cosine matching
# =============================================================================

def load_profiles() -> dict[str, np.ndarray]:
    """Read every speaker_profiles/<name>.npy. Returns {name: embedding}."""
    if not PROFILES_DIR.exists():
        log.warning(
            "No %s directory; running without speaker fingerprinting. "
            "All speakers will be labelled 'unknown'. Run enroll_speaker.py first.",
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
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def best_match(
    embedding: np.ndarray,
    profiles: dict[str, np.ndarray],
    threshold: float = MATCH_THRESHOLD,
) -> tuple[str, float]:
    """Return (matched_name, score). matched_name == 'unknown' if no
    profile crosses the similarity threshold."""
    if not profiles:
        return ("unknown", 0.0)
    scores = [(name, cosine(embedding, prof)) for name, prof in profiles.items()]
    scores.sort(key=lambda t: t[1], reverse=True)
    top_name, top_score = scores[0]
    if top_score >= threshold:
        return (top_name, top_score)
    return ("unknown", top_score)


# =============================================================================
# Audio handling — extract per-speaker audio for embedding
# =============================================================================

def speaker_audio_samples(
    audio: np.ndarray,
    sample_rate: int,
    segments: list[dict],
    max_seconds_per_speaker: float = 30.0,
) -> dict[str, np.ndarray]:
    """For each anonymous speaker label (SPEAKER_00, _01, ...), return
    concatenated audio samples up to max_seconds_per_speaker. Used as
    input to the embedding model — embeddings stabilize quickly past
    ~10-15 seconds of speech, so 30s gives headroom without ballooning
    memory."""
    by_speaker: dict[str, list[np.ndarray]] = {}
    duration_so_far: dict[str, float] = {}
    for seg in segments:
        spk = seg.get("speaker")
        if not spk:
            continue
        if duration_so_far.get(spk, 0.0) >= max_seconds_per_speaker:
            continue
        start_sample = int(seg["start"] * sample_rate)
        end_sample = int(seg["end"] * sample_rate)
        chunk = audio[start_sample:end_sample]
        if chunk.size == 0:
            continue
        by_speaker.setdefault(spk, []).append(chunk)
        duration_so_far[spk] = duration_so_far.get(spk, 0.0) + (seg["end"] - seg["start"])
    return {spk: np.concatenate(chunks) for spk, chunks in by_speaker.items() if chunks}


# =============================================================================
# Main processing
# =============================================================================

def process_one(
    ogg_path: Path,
    profiles: dict[str, np.ndarray],
    asr_model,
    diarize_pipeline,
    embedding_inference,
    align_model_cache: dict,
) -> dict:
    """Process a single OGG. Returns the enriched-transcript dict."""
    import whisperx

    log.info("Loading audio: %s", ogg_path.name)
    audio = whisperx.load_audio(str(ogg_path))
    duration = len(audio) / 16000.0  # whisperx loads at 16 kHz

    # ---- 1. Transcribe ---------------------------------------------------
    log.info("  Transcribing (Whisper %s, %s/%s)...",
             WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE)
    result = asr_model.transcribe(audio, batch_size=16)
    language = result.get("language", "en")

    # ---- 2. Word-level alignment (gives precise timestamps for
    #        diarization assignment) ----
    align_key = language
    if align_key not in align_model_cache:
        am, am_meta = whisperx.load_align_model(
            language_code=align_key, device=WHISPER_DEVICE,
        )
        align_model_cache[align_key] = (am, am_meta)
    align_model, align_meta = align_model_cache[align_key]
    log.info("  Aligning words ...")
    aligned = whisperx.align(
        result["segments"], align_model, align_meta, audio,
        device=WHISPER_DEVICE, return_char_alignments=False,
    )

    # ---- 3. Diarize ------------------------------------------------------
    log.info("  Diarizing speakers ...")
    diarize_segments = diarize_pipeline(str(ogg_path))
    assigned = whisperx.assign_word_speakers(diarize_segments, aligned)
    segments = assigned.get("segments", [])

    # Strip word-level entries — too verbose for the transcript JSON.
    for seg in segments:
        seg.pop("words", None)

    # ---- 4. Embed each anonymous speaker, match to enrolled profile ----
    speakers_present: dict[str, dict] = {}
    if profiles:
        log.info("  Computing speaker embeddings + matching to %d profiles ...",
                 len(profiles))
        # pyannote Inference works from a file path or an in-memory waveform.
        # Easiest: dump each speaker's concatenated audio to a temp wav and
        # feed the path in. Alternatively, build a SlidingWindowFeature.
        import tempfile
        import soundfile as sf

        by_speaker_audio = speaker_audio_samples(audio, 16000, segments)
        for spk_label, samples in by_speaker_audio.items():
            with tempfile.NamedTemporaryFile(
                suffix=".wav", delete=False,
            ) as tmp:
                sf.write(tmp.name, samples, 16000)
                tmp_path = tmp.name
            try:
                emb = embedding_inference(tmp_path)
                emb = np.asarray(emb).reshape(-1)
                matched_name, score = best_match(emb, profiles)
                speakers_present[spk_label] = {
                    "matched_name": matched_name,
                    "match_score": round(score, 3),
                    "audio_seconds": round(len(samples) / 16000.0, 2),
                }
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    else:
        # No profiles — every anonymous speaker stays "unknown"
        for seg in segments:
            spk = seg.get("speaker")
            if spk and spk not in speakers_present:
                speakers_present[spk] = {
                    "matched_name": "unknown",
                    "match_score": 0.0,
                    "audio_seconds": None,
                }

    # ---- 5. Annotate each segment with the matched name ---------------
    for seg in segments:
        spk = seg.get("speaker")
        info = speakers_present.get(spk) if spk else None
        if info:
            seg["matched_name"] = info["matched_name"]
            seg["match_score"] = info["match_score"]
        else:
            seg["matched_name"] = "unknown"
            seg["match_score"] = 0.0

    # Compact speakers_present for the top-level summary.
    speakers_summary = {
        spk: info["matched_name"] for spk, info in speakers_present.items()
    }

    return {
        "source_ogg": ogg_path.name,
        "processed_at": datetime.now().isoformat(),
        "duration_seconds": round(duration, 2),
        "language": language,
        "match_threshold": MATCH_THRESHOLD,
        "speakers_present": speakers_summary,
        "speakers_detail": speakers_present,
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
    import whisperx
    from pyannote.audio import Model, Inference

    log.info("Loading WhisperX model: %s (%s/%s) ...",
             WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE)
    asr_model = whisperx.load_model(
        WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE,
    )

    log.info("Loading diarization pipeline ...")
    diarize_pipeline = whisperx.DiarizationPipeline(
        use_auth_token=HF_TOKEN, device=WHISPER_DEVICE,
    )

    log.info("Loading speaker embedding model ...")
    import torch
    emb_model = Model.from_pretrained(
        "pyannote/embedding", use_auth_token=HF_TOKEN,
    )
    embedding_inference = Inference(
        emb_model, window="whole",
        device=torch.device(WHISPER_DEVICE),
    )

    align_model_cache: dict = {}

    succeeded = failed = 0
    for ogg in pending:
        try:
            t0 = datetime.now()
            enriched = process_one(
                ogg, profiles, asr_model, diarize_pipeline,
                embedding_inference, align_model_cache,
            )
            out_json = OUTPUT_DIR / f"{ogg.stem}.json"
            out_json.write_text(
                json.dumps(enriched, indent=2, default=str),
                encoding="utf-8",
            )
            elapsed = (datetime.now() - t0).total_seconds()
            log.info(
                "  -> %s  (%.1fs wall, %d speakers, %d segments)",
                out_json.name, elapsed,
                len(enriched.get("speakers_present", {})),
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
