"""Minimal LiveKit agent: greet the caller and chat briefly.

Verifies the audio pipeline works end-to-end through the PSTN:
    Twilio inbound -> LiveKit SIP -> room -> this agent -> Deepgram STT
                                                       -> Claude Haiku LLM
                                                       -> Deepgram Aura TTS
                                                       -> back to caller

Run on the droplet from /opt/iris-backend/agent/:
    .venv/bin/python hello_agent.py dev

Required env vars (in agent/.env):
    LIVEKIT_URL          ws://127.0.0.1:7880
    LIVEKIT_API_KEY      from /opt/livekit/livekit.yaml
    LIVEKIT_API_SECRET   from /opt/livekit/livekit.yaml
    ANTHROPIC_API_KEY    same as backend/.env
    DEEPGRAM_API_KEY     from console.deepgram.com
"""
import logging

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentSession
from livekit.plugins import anthropic, deepgram, silero

load_dotenv()
log = logging.getLogger("iris-hello")
logging.basicConfig(level=logging.INFO)


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
        # Aura is Deepgram's TTS — same vendor as STT, one API key for both.
        # We'll swap this for a self-hosted Kokoro adapter once that's built.
        tts=deepgram.TTS(model="aura-2-thalia-en"),
    )

    await session.start(room=ctx.room, agent=HelloAgent())


if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint))
