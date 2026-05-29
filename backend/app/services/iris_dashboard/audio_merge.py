"""Merge per-participant OGG recordings into a single stereo OGG.

LiveKit's egress writes one OGG per published track, so each call leaves
us with 2 OGGs (one for the caller's mic, one for the agent's TTS).
For the dashboard we want a single audio file where you can hear both
sides — caller on the left channel, Iris on the right.

We don't have a reliable signal in the filename for which OGG is which
track (the TR_ id is opaque). For now we adopt a heuristic:
  - Both OGGs are timestamped at egress start.
  - We just put one in each channel based on alphabetical track-id sort.
  - The frontend has a "swap channels" button so the user can flip it.

ffmpeg is invoked via subprocess; we don't add a python-ffmpeg dep.
The merge happens once per call, cached on disk as `merged_{call_id}.ogg`.
If both source OGGs are present and the merged file is newer than both,
we skip the merge.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)


async def _run_ffmpeg(args: list[str], timeout_s: float = 60.0) -> tuple[int, str, str]:
    """Run ffmpeg as a subprocess, returning (return_code, stdout, stderr).

    ffmpeg is found via PATH; if it's not installed the merge fails
    gracefully (the route layer falls back to serving the two OGGs
    separately).
    """
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
    """Cheap check used by the route layer to know whether merging is possible."""
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


async def merge_to_stereo(
    sources: list[Path],
    out_path: Path,
    *,
    swap_channels: bool = False,
    force: bool = False,
) -> bool:
    """Merge two source OGGs into a stereo OGG.

    sources: list of source OGG paths. We expect exactly 2; anything else
        is logged as a warning and the function returns False without
        producing output.
    out_path: where to write the merged stereo OGG.
    swap_channels: if True, swap which source becomes left vs right.
    force: re-merge even if out_path is newer than both sources.

    Returns True on success, False on any failure. Caller decides
    whether to fall back (e.g., serve sources separately) on False.
    """
    if not ffmpeg_available():
        log.warning("ffmpeg not in PATH; cannot merge OGGs to stereo")
        return False
    if len(sources) != 2:
        log.warning(
            "merge_to_stereo: expected 2 source OGGs, got %d (%s)",
            len(sources), [str(s) for s in sources],
        )
        return False
    for s in sources:
        if not s.exists():
            log.warning("merge_to_stereo: source missing: %s", s)
            return False

    if not force and not _needs_remerge(out_path, sources):
        # Cached file is fresh; nothing to do.
        return True

    out_path.parent.mkdir(parents=True, exist_ok=True)

    left, right = (sources[1], sources[0]) if swap_channels else (sources[0], sources[1])

    # ffmpeg filter_complex: map left source to channel 0, right to channel 1.
    # `amerge` mixes; we use it with FL/FR mapping. -ac 2 forces stereo output.
    # Output is Opus in OGG (matches LiveKit egress format) so browsers can
    # decode it directly without a re-encode UI step.
    args = [
        "ffmpeg",
        "-y",                       # overwrite output
        "-i", str(left),
        "-i", str(right),
        "-filter_complex",
        "[0:a]channelmap=channel_layout=mono[L];"
        "[1:a]channelmap=channel_layout=mono[R];"
        "[L][R]amerge=inputs=2,pan=stereo|FL=c0|FR=c1[out]",
        "-map", "[out]",
        "-c:a", "libopus",
        "-b:a", "48k",
        "-ac", "2",
        str(out_path),
    ]
    rc, stdout, stderr = await _run_ffmpeg(args, timeout_s=60.0)
    if rc != 0:
        log.warning(
            "ffmpeg merge failed (rc=%d) for %s + %s -> %s\nstderr: %s",
            rc, left, right, out_path, stderr[-500:],
        )
        # Clean up a half-written file if any
        try:
            if out_path.exists():
                out_path.unlink()
        except OSError:
            pass
        return False

    log.info("Merged %s + %s -> %s", left.name, right.name, out_path.name)
    return True
