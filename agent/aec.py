"""Wire WebRTC AEC (livekit.rtc.apm.AudioProcessingModule) into the
agent's input/output audio path.

WHY: The framework's RoomIO already creates an rtc.AudioProcessingModule
internally for the input side, but echo_cancellation is hard-coded OFF
(only auto_gain_control is enabled, see
/opt/iris-backend/agent/.venv/lib/python3.12/site-packages/livekit/agents/voice/room_io/_input.py:284-286).
There's also no reverse-stream wiring, which AEC needs to know what the
agent has just played so it can cancel that audio out of the incoming
caller stream.

This module replaces the framework's APM with a full WebRTC suite
(echo_cancellation + noise_suppression + auto_gain_control +
high_pass_filter) and adds the missing reverse-stream feed.

SELF-ECHO PATHOLOGY THIS FIXES (both observed on real calls):
  1. VAD-false-positive burble: framework's _interrupt_by_audio_activity
     fires when VAD detects "speech" in the caller's track that is
     actually Iris's own TTS bleeding back through the SIP carrier.
     The path pauses audio output mid-frame, clears the publish queue,
     and resumes ~2s later via the false_interruption_timer. Caller
     hears a mid-word burble. See transcript_20260529_190841_15419973221.json
     around t=24-31 (the "Just to confirm..." utterance).
  2. STT phantom user turns: Deepgram transcribes Iris's echoed voice
     as the caller's input. Framework generates a phantom user turn,
     LLM responds, TTS plays -- caller hears Iris repeat a closing
     phrase. See transcript_20260529_173638_15419973221.json at the
     duplicated "Is there anything else I can help you with today?".

Both pathologies vanish if VAD/STT never sees Iris's voice in the input
stream. APM is the cleanest way to make that happen on a self-hosted
deployment (LiveKit Cloud's Krisp is the equivalent for hosted setups
but is a no-op on self-hosted droplets).

USAGE:
    # In on_enter, AFTER framework has populated session.input/output:
    import aec
    aec.enable_aec(self.session, delay_ms=100)
    # Then proceed with greeting playback. The greeting's outgoing
    # frames will be fed into APM's reverse stream from the first frame.

TUNING:
    delay_ms is the empirical round-trip delay from when an audio frame
    is emitted by the agent to when its echo arrives back in the
    caller's input stream. Twilio -> SIP carrier -> LiveKit-SIP ->
    agent typically lands in the 80-150ms range. The default 100ms is
    a reasonable starting point; if AEC performance is poor, try
    50, 80, 120, 150ms in test calls and pick whichever sounds best.

SAFETY:
    All failures in this module are caught and logged. The call
    continues without AEC if anything goes wrong. Gated by the
    IRIS_AEC_ENABLED env var (defaults to "true"); set to "false"
    to disable cleanly without removing the wiring.
"""
import logging
import os

from livekit import rtc
from livekit.agents import AgentSession

log = logging.getLogger("iris.aec")

# Empirical starting point for Twilio -> SIP -> LiveKit round-trip
# delay. The right answer depends on the specific carrier path and
# can be measured on a real call (correlate timestamp of an outgoing
# TTS event with timestamp of the corresponding echo in the input).
DEFAULT_DELAY_MS = 100


def enable_aec(session: AgentSession, *, delay_ms: int = DEFAULT_DELAY_MS) -> bool:
    """Enable echo cancellation on the agent's input + output audio path.

    Must be called AFTER session.start() has populated the input/output
    audio handles. The right call site for Iris is inside
    `IrisAgent.on_enter`, before the first session.say() so the greeting
    is captured into APM's reverse stream from frame zero.

    Returns True if AEC was wired, False if anything was missing or
    the env-var disable flag was set. Never raises -- a failure to
    wire AEC is logged and the call continues without it.

    Wiring details (kept here for the next person debugging this):

    1. INPUT SIDE. The framework's `_ParticipantAudioInputStream` has
       a private `_apm` attribute that's set to APM(auto_gain_control=True)
       at construction. Each incoming frame is passed through
       `self._apm.process_stream(frame)` in `_process_frame`. We
       replace `_apm` with our full-suite APM instance; from the
       next frame on, the same code path runs OUR APM.

    2. OUTPUT SIDE. The framework's `_ParticipantAudioOutput` owns an
       `rtc.AudioSource` that publishes frames to the room. Each
       outgoing 50ms frame goes through `_audio_source.capture_frame`.
       We replace that bound method on the instance with a wrapper
       that calls `apm.process_reverse_stream(frame)` first, then
       the original capture_frame -- so the frame goes to the room
       unmodified but APM also gets a reference copy.

       process_reverse_stream() modifies the frame's data in-place,
       but per the WebRTC APM contract it only stores the audio
       internally for use during the next process_stream() call.
       We rely on the FRAMEWORK calling capture_frame in order;
       any out-of-order capture would break AEC timing.

    3. STREAM DELAY. APM's AEC needs to know the round-trip latency
       between far-end playout and near-end capture so it can align
       the cancellation. We set it once at startup; WebRTC's AEC
       also has some adaptive behavior to handle small drift.
    """
    if os.environ.get("IRIS_AEC_ENABLED", "true").strip().lower() != "true":
        log.info("AEC disabled via IRIS_AEC_ENABLED env var")
        return False

    input_audio = getattr(session.input, "audio", None)
    output_audio = getattr(session.output, "audio", None)
    if input_audio is None:
        log.warning("AEC: session.input.audio is None; skipping")
        return False
    if output_audio is None:
        log.warning("AEC: session.output.audio is None; skipping")
        return False

    audio_source = getattr(output_audio, "_audio_source", None)
    if audio_source is None:
        log.warning(
            "AEC: no _audio_source on output (framework version may have "
            "moved it); skipping"
        )
        return False

    # Refuse to wire twice -- a second call would chain wrappers around
    # an already-wrapped capture_frame, doubling reverse_stream feeds.
    if getattr(audio_source, "_iris_aec_wired", False):
        log.info("AEC: already wired on this audio_source; skipping")
        return True

    # Build the APM with the full WebRTC suite. Each flag is independent:
    #   echo_cancellation: cancels reverse_stream from process_stream.
    #     This is the headline feature for our use case.
    #   noise_suppression: drops background noise from the caller. Mild
    #     quality win; should not interfere with STT accuracy.
    #   auto_gain_control: matches the framework's default (it had AGC
    #     on its own APM, so disabling here would be a regression).
    #   high_pass_filter: removes sub-80Hz rumble. Standard hygiene.
    aec_apm = rtc.AudioProcessingModule(
        echo_cancellation=True,
        noise_suppression=True,
        auto_gain_control=True,
        high_pass_filter=True,
    )
    aec_apm.set_stream_delay_ms(int(delay_ms))

    # 1. Replace the framework's input-side APM. The framework reads
    # input_audio._apm on every incoming frame in _process_frame, so
    # swapping the attribute takes effect immediately for the next
    # frame the input pipeline pulls.
    input_audio._apm = aec_apm

    # 2. Hook the output-side audio_source. We monkey-patch the bound
    # method on the instance (NOT on the class) so we don't affect any
    # other rtc.AudioSource that might exist elsewhere in the process.
    original_capture_frame = audio_source.capture_frame

    async def aec_capture_frame(frame: rtc.AudioFrame) -> None:
        try:
            aec_apm.process_reverse_stream(frame)
        except Exception:
            # Don't let AEC bugs break TTS playout. If APM somehow
            # rejects a frame, the room still gets the original audio.
            log.exception(
                "AEC: process_reverse_stream failed for frame "
                "(sr=%s, ch=%s, spc=%s); continuing playout",
                getattr(frame, "sample_rate", "?"),
                getattr(frame, "num_channels", "?"),
                getattr(frame, "samples_per_channel", "?"),
            )
        await original_capture_frame(frame)

    audio_source.capture_frame = aec_capture_frame
    audio_source._iris_aec_wired = True

    log.info(
        "AEC enabled: echo_cancellation+noise_suppression+AGC+HPF, "
        "stream_delay=%dms",
        delay_ms,
    )
    return True
