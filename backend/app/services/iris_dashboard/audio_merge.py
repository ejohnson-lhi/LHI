"""Merge per-participant OGG recordings into a single stereo OGG.

LiveKit's egress writes one OGG per published track. A call has:
  - 2 tracks for a normal AI-only call (caller, Iris)
  - 3 tracks if the call was transferred (caller, Iris, answerer).
    Iris's track exists for the whole call; the answerer's track exists
    only for the transferred portion. They don't overlap in time (Iris
    stops speaking once the transfer connects, and her audio output
    gets muted -- see set_audio_enabled(False) in iris_agent.py around
    the connected branch of transfer_to).

For the dashboard we want a single audio file where you can hear all
participants:
  - 2 tracks: caller -> left channel, Iris -> right channel.
  - 3 tracks: caller -> left channel, Iris + answerer mixed -> right.
    Since Iris and answerer don't overlap, the right channel plays
    Iris during the AI portion and the answerer during the human
    portion -- as one continuous "other party" track.

When roles are unknown (old format, pre-2026-05-29), we fall back to
alphabetical track_id ordering and a swap button in the UI.

ffmpeg is invoked via subprocess; we don't add a python-ffmpeg dep.
The merge happens once per call, cached on disk as `merged_{call_id}.ogg`.
If sources don't change, we skip re-merging.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)


async def _run_ffmpeg(args: list[str], timeout_s: float = 60.0) -> tuple[int, str, str]:
    """Run ffmpeg as a subprocess, returning (return_code, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return (124, "", "ffmpeg timed out")
    return (proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace"))


def ffmpeg_available() -> bool:
    """True if ffmpeg is on PATH. Route layer uses this to know whether
    to attempt merging at all."""
    return shutil.which("ffmpeg") is not None


def _needs_remerge(out_path: Path, source_paths: list[Path]) -> bool:
    """True if the merged file is missing or older than any source."""
    if not out_path.exists():
        return True
    out_mtime = out_path.stat().st_mtime
    for s in source_paths:
        if s.stat().st_mtime > out_mtime:
            return True
    return False


def _choose_left_right(
    tracks: list[dict],
) -> tuple[list[Path], list[Path]] | None:
    """Given track dicts with `path` + `role` fields, decide which OGG(s)
    go into the LEFT and RIGHT channels of the merged stereo output.

    Strategy:
      - LEFT: all tracks with role == "caller" (typically just one).
      - RIGHT: all tracks with role in {"iris", "answerer"} (one or two,
        never overlapping in time in our call flow).
      - If no role info at all (old recordings), return None and let
        the caller fall back to alphabetical-order stereo.
      - If we have role info but neither side has any tracks, return None.

    Returns ([left_path, ...], [right_path, ...]) or None.
    """
    have_roles = any(t["role"] != "unknown" for t in tracks)
    if not have_roles:
        return None
    left = [Path(t["path"]) for t in tracks if t["role"] == "caller"]
    right = [Path(t["path"]) for t in tracks if t["role"] in ("iris", "answerer")]
    # Unknown-role tracks go to right by default — better to hear them
    # than drop them. Users can still see them as separate tracks in
    # the fallback per-track players.
    right += [Path(t["path"]) for t in tracks if t["role"] == "unknown"]
    if not left or not right:
        return None
    return left, right


async def merge_to_stereo(
    tracks: list[dict],
    out_path: Path,
    *,
    swap_channels: bool = False,
    force: bool = False,
) -> bool:
    """Merge per-participant OGGs into a stereo OGG, role-aware.

    tracks: list of dicts with at least {"path": str, "role": str}.
        Typically 2-3 entries. Other fields (track_id, identity, label)
        are ignored here.
    out_path: where to write the merged stereo OGG.
    swap_channels: if True, swap LEFT and RIGHT after role assignment.
        Useful when our role guess is wrong.
    force: re-merge even if out_path is newer than all sources.

    Returns True on success, False if we couldn't produce a merge.
    """
    if not ffmpeg_available():
        log.warning("ffmpeg not in PATH; cannot merge OGGs")
        return False
    if not tracks:
        return False
    source_paths = [Path(t["path"]) for t in tracks]
    for s in source_paths:
        if not s.exists():
            log.warning("merge_to_stereo: source missing: %s", s)
            return False

    if not force and not _needs_remerge(out_path, source_paths):
        return True

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Role-based channel assignment (preferred).
    choice = _choose_left_right(tracks)
    if choice is None:
        # Old format: no role info. Fall back to alphabetical track_id
        # ordering -- we need to use a different signal. The caller
        # passed tracks in some order; assume index 0 -> left, 1+ -> right.
        if len(source_paths) < 2:
            log.warning("merge_to_stereo: <2 tracks and no role info; cannot stereo-merge")
            return False
        left_paths = [source_paths[0]]
        right_paths = source_paths[1:]
    else:
        left_paths, right_paths = choice

    if swap_channels:
        left_paths, right_paths = right_paths, left_paths

    # Build the ffmpeg filter graph:
    #   - Each input is mono.
    #   - If multiple sources on one side, amix them with sum_to_first
    #     normalization (sum the streams; the 'longest' duration flag
    #     keeps the output as long as the longest input).
    #   - Then channelmap + amerge to produce stereo with the L/R sides.
    inputs: list[Path] = []
    parts: list[str] = []

    def _mix_side(side_paths: list[Path], label: str) -> str:
        """Returns the filter-graph label for the mixed side audio."""
        start_idx = len(inputs)
        for p in side_paths:
            inputs.append(p)
        if len(side_paths) == 1:
            parts.append(f"[{start_idx}:a]aformat=channel_layouts=mono[{label}]")
        else:
            # amix multiple inputs. duration=longest so we don't truncate
            # to the shortest input. dropout_transition=0 to avoid level
            # pumping when one source ends.
            tags = "".join(f"[{i}:a]" for i in range(start_idx, len(inputs)))
            parts.append(
                f"{tags}amix=inputs={len(side_paths)}:duration=longest:"
                f"dropout_transition=0,aformat=channel_layouts=mono[{label}]"
            )
        return label

    left_label = _mix_side(left_paths, "L")
    right_label = _mix_side(right_paths, "R")

    # Combine into stereo. amerge expects inputs to already be mono;
    # we use pan to explicitly assign L=c0, R=c1.
    parts.append(
        f"[{left_label}][{right_label}]amerge=inputs=2,pan=stereo|FL=c0|FR=c1[out]"
    )
    filter_complex = ";".join(parts)

    args = ["ffmpeg", "-y"]
    for p in inputs:
        args += ["-i", str(p)]
    args += [
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-c:a", "libopus",
        "-b:a", "48k",
        "-ac", "2",
        str(out_path),
    ]

    rc, _stdout, stderr = await _run_ffmpeg(args, timeout_s=120.0)
    if rc != 0:
        log.warning(
            "ffmpeg merge failed (rc=%d) for %d inputs -> %s\nstderr tail: %s",
            rc, len(inputs), out_path, stderr[-500:],
        )
        try:
            if out_path.exists():
                out_path.unlink()
        except OSError:
            pass
        return False

    log.info(
        "Merged %d tracks (L=%d, R=%d) -> %s",
        len(inputs), len(left_paths), len(right_paths), out_path.name,
    )
    return True
