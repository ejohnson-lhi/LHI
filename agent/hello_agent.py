"""Minimal LiveKit agent: greet the caller and chat briefly.

Verifies the audio pipeline works end-to-end through the PSTN:
    Twilio inbound -> LiveKit SIP -> room -> this agent -> Deepgram STT
                                                       -> Claude Haiku LLM
                                                       -> Kokoro TTS (af_sarah)
                                                       -> back to caller

Run on the droplet from /opt/iris-backend/agent/:
    .venv/bin/python hello_agent.py dev      # foreground, file-watcher reload
    .venv/bin/python hello_agent.py start    # foreground, no reload (systemd)

Required env vars (in agent/.env):
    LIVEKIT_URL          ws://127.0.0.1:7880
    LIVEKIT_API_KEY      from /opt/livekit/livekit.yaml
    LIVEKIT_API_SECRET   from /opt/livekit/livekit.yaml
    ANTHROPIC_API_KEY    same as backend/.env
    DEEPGRAM_API_KEY     from console.deepgram.com

Required Kokoro model files in agent/models/ (gitignored):
    kokoro-v1.0.onnx     ~325 MB, get from
                         https://github.com/thewh1teagle/kokoro-onnx/releases
    voices-v1.0.bin      ~36 MB, same release

Required system package:
    sudo apt install espeak-ng    # phonemizer used by kokoro-onnx
"""
import logging
from pathlib import Path

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentSession
from livekit.agents.voice.turn import InterruptionOptions, TurnHandlingOptions
from livekit.plugins import anthropic, deepgram, silero

from kokoro_tts import KokoroTTS

load_dotenv()
log = logging.getLogger("iris-hello")
logging.basicConfig(level=logging.INFO)

HERE = Path(__file__).parent
KOKORO_MODEL = HERE / "models" / "kokoro-v1.0.onnx"
KOKORO_VOICES = HERE / "models" / "voices-v1.0.bin"


class HelloAgent(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are Iris, a friendly AI receptionist for the Lighthouse Inn "
                "in Florence, Oregon. This is just a connectivity test. Greet the "
                "caller warmly, ask how you can help, and acknowledge whatever they "
                "say. Do NOT pretend to take reservations or look anything up. Keep "
                "responses to one sentence."
            )
        )

    async def on_enter(self) -> None:
        # `on_enter` fires after the AgentSession is fully wired to the room.
        # Greeting from inside `entrypoint` (after `session.start`) sometimes
        # got swallowed during the AEC warmup window — this avoids that.
        log.info("Agent on_enter: generating greeting")
        await self.session.generate_reply(
            instructions="Greet the caller warmly as Iris and ask how you can help."
        )


async def entrypoint(ctx: agents.JobContext) -> None:
    log.info("Agent connecting to room %s", ctx.room.name)
    await ctx.connect()

    session = AgentSession(
        vad=silero.VAD.load(),
        stt=deepgram.STT(model="nova-3"),
        llm=anthropic.LLM(model="claude-haiku-4-5"),
        # Self-hosted Kokoro TTS. Kokoro is cpu-bound but ONNX Runtime
        # releases the GIL, and the adapter offloads to a thread pool, so
        # synthesis doesn't block the event loop.
        tts=KokoroTTS(
            model_path=str(KOKORO_MODEL),
            voices_path=str(KOKORO_VOICES),
            voice="af_sarah",
        ),
        # Skip the LiveKit Cloud "adaptive interruption" detector — it's a
        # cloud-only feature that 401s against our self-hosted setup, costs
        # ~5 sec at startup retrying, and falls back to VAD anyway.
        turn_handling=TurnHandlingOptions(
            interruption=InterruptionOptions(mode="vad"),
        ),
    )

    await session.start(room=ctx.room, agent=HelloAgent())


if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint))
