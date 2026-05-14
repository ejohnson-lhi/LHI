"""Nightly batch: diarize + transcribe new call recordings, attach
speaker fingerprint matches, write enriched JSON.

Idempotent: scans recordings/ for OGGs without a sibling
recordings/transcribed/<basename>.json, processes only the new ones.

Run manually:
    source /opt/iris-backend/tools/diarize/.venv/bin/activate
    python diarize_batch.py

Or via cron (see README.md).

Pipeline (refactored to use library primitives directly — no whisperx
wrapper, which had hardcoded URLs that broke when servers moved):
  1. faster-whisper large-v3 (int8) + built-in VAD → segments with
     timestamps and text
  2. pyannote.audio Pipeline (speaker-diarization-3.1) → annotated
     time-line with anonymous speaker labels (SPEAKER_00, _01, ...)
  3. For each transcription segment, find the speaker label whose
     diarization turns cover the most of that segment's duration
  4. Per anonymous speaker, extract a representative audio sample
     (concatenated turns, capped at 30s) and compute a pyannote
     embedding → match against enrolled speaker_profiles/*.npy via
     cosine similarity. Threshold (default 0.55) controls "known vs
     unknown" cutoff. Note for pyannote 4.x: the WeSpeaker embedder
     produces 512-dim vectors that are NOT pre-normalized; cosine
     normalizes internally so this is correct, but if you migrate
     between embedder versions you'll need to re-enroll everyone.
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
    """Cosine similarity in [-1, 1]. Both inputs treated as 1-D.
    Normalizes internally so non-normalized embeddings (e.g. pyannote 4.x
    WeSpeaker 512-dim vectors) work the same as normalized ones (3.x
    ECAPA-TDNN 192-dim vectors). Both vectors must be from the same
    embedder, however."""
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
# Speaker-segment assignment
# =============================================================================

def assign_speaker(
    segment_start: float,
    segment_end: float,
    diarization,
) -> str | None:
    """For a (start, end) interval, return the speaker label whose
    diarization turns cover the most of that interval. None if no
    overlap.

    `diarization` is a `pyannote.core.Annotation` instance — yields
    (turn: Segment, _, label) tuples via .itertracks(yield_label=True).
    """
    overlaps: dict[str, float] = {}
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        overlap_start = max(turn.start, segment_start)
        overlap_end = min(turn.end, segment_end)
        if overlap_end > overlap_start:
            overlaps[speaker] = overlaps.get(speaker, 0.0) + (overlap_end - overlap_start)
    if not overlaps:
        return None
    return max(overlaps.items(), key=lambda kv: kv[1])[0]


def speaker_audio_samples(
    audio: np.ndarray,
    sample_rate: int,
    diarization,
    max_seconds_per_speaker: float = 30.0,
) -> dict[str, np.ndarray]:
    """For each speaker in the diarization, return concatenated audio
    samples up to max_seconds_per_speaker. Used to compute one embedding
    per speaker for fingerprint matching. Embeddings stabilize past
    ~10-15s of speech so 30s is plenty without ballooning memory."""
    by_speaker: dict[str, list[np.ndarray]] = {}
    duration_so_far: dict[str, float] = {}
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        if duration_so_far.get(speaker, 0.0) >= max_seconds_per_speaker:
            continue
        start_sample = int(turn.start * sample_rate)
        end_sample = int(turn.end * sample_rate)
        chunk = audio[start_sample:end_sample]
        if chunk.size == 0:
            continue
        by_speaker.setdefault(speaker, []).append(chunk)
        duration_so_far[speaker] = duration_so_far.get(speaker, 0.0) + (turn.end - turn.start)
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
) -> dict:
    """Process a single OGG. Returns the enriched-transcript dict."""
    import librosa

    log.info("Loading audio: %s", ogg_path.name)
    # librosa.load() does ffmpeg-backed decoding + automatic resample.
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
    # faster-whisper returns a generator; materialize so we can iterate twice.
    fw_segments = list(fw_segments_iter)
    language = getattr(info, "language", "en")

    # ---- 2. Diarize with pyannote.audio Pipeline ---------------------
    log.info("  Diarizing speakers ...")
    diarization = diarize_pipeline(str(ogg_path))

    # ---- 3. Combine: assign a speaker label to each whisper segment ------
    segments = []
    for fs in fw_segments:
        spk = assign_speaker(fs.start, fs.end, diarization)
        segments.append({
            "start": round(fs.start, 2),
            "end": round(fs.end, 2),
            "text": (fs.text or "").strip(),
            "speaker": spk,
        })

    # ---- 4. Embed each anonymous speaker, match to enrolled profile -----
    speakers_present: dict[str, dict] = {}
    if profiles:
        log.info("  Computing speaker embeddings + matching to %d profiles ...",
                 len(profiles))
        by_speaker_audio = speaker_audio_samples(audio, sr, diarization)
        for spk_label, samples in by_speaker_audio.items():
            # Write the speaker's concatenated audio to a temp wav so
            # the pyannote Inference can read it from disk (its file-path
            # interface is more reliable than the in-memory waveform one
            # across pyannote versions).
            with tempfile.NamedTemporaryFile(
                suffix=".wav", delete=False,
            ) as tmp:
                sf.write(tmp.name, samples, sr)
                tmp_path = tmp.name
            try:
                emb = embedding_inference(tmp_path)
                emb = np.asarray(emb).reshape(-1)
                matched_name, score = best_match(emb, profiles)
                speakers_present[spk_label] = {
                    "matched_name": matched_name,
                    "match_score": round(score, 3),
                    "audio_seconds": round(len(samples) / sr, 2),
                    "embedding_dim": int(emb.size),
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

    # ---- 5. Annotate each segment with the matched name ------------------
    for seg in segments:
        spk = seg.get("speaker")
        info_d = speakers_present.get(spk) if spk else None
        if info_d:
            seg["matched_name"] = info_d["matched_name"]
            seg["match_score"] = info_d["match_score"]
        else:
            seg["matched_name"] = "unknown"
            seg["match_score"] = 0.0

    speakers_summary = {
        spk: data["matched_name"] for spk, data in speakers_present.items()
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
    from faster_whisper import WhisperModel
    from pyannote.audio import Model, Inference, Pipeline
    import torch

    log.info("Loading faster-whisper model: %s (%s/%s) ...",
             WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE)
    asr_model = WhisperModel(
        WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE,
    )

    log.info("Loading pyannote diarization pipeline ...")
    # pyannote.audio 4.x renamed `use_auth_token` to `token` in
    # Pipeline.from_pretrained (Model.from_pretrained still accepts both).
    # Try the new name first, fall back for 3.x compatibility.
    try:
        diarize_pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            token=HF_TOKEN,
        )
    except TypeError:
        diarize_pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            use_auth_token=HF_TOKEN,
        )
    # Pipeline.to() exists in pyannote 3.x and 4.x.
    try:
        diarize_pipeline.to(torch.device(WHISPER_DEVICE))
    except Exception:
        log.warning(
            "Could not move diarization pipeline to %s; defaults will apply.",
            WHISPER_DEVICE,
        )

    log.info("Loading pyannote speaker embedding model ...")
    # Same compatibility shim as above. Model.from_pretrained accepts
    # both names today, but use the canonical 4.x name for forward compat.
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
                ogg, profiles, asr_model, diarize_pipeline, embedding_inference,
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
