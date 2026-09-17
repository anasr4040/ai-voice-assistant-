"""The voice pipeline: phone audio in, spoken German out.

Cascaded on purpose (STT -> LLM -> TTS) rather than a realtime speech-to-speech
model. It costs roughly a fifth as much per minute, and every stage is
swappable, which matters because German TTS quality is the thing the owner will
judge this on. Swap providers with env vars, not code edits.

Run it::

    uv run bot.py -t twilio -x <your-ngrok-host>   # phone
    uv run bot.py                                  # browser, no phone needed
"""

from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from pipecat.workers.runner import WorkerRunner

from receptionist import prompts, store, telephony
from receptionist.tools import TOOLS, CallSession

load_dotenv(override=True)

# Telephony is 8 kHz mu-law end to end; the browser demo can afford 16 kHz.
TELEPHONY_SAMPLE_RATE = 8000
WEB_SAMPLE_RATE = 16000

# Hard ceiling on one call. Pipecat already hangs up after 300s of SILENCE, but
# nothing stops a call that keeps producing audio -- a stuck line, a radio left
# on, a caller who will not stop. Every one of those minutes bills speech-to-text
# and text-to-speech, so cap it. 0 disables.
MAX_CALL_SECONDS = int(os.getenv("MAX_CALL_SECONDS", "600"))


def build_stt():
    """Deepgram, configured for a German caller who may switch to English.

    nova-3 with language="multi" handles code-switching inside one sentence,
    which is what actually happens in Hamburg ("Ich brauche den Fuehrerschein,
    but I only speak English").
    """
    from pipecat.services.deepgram.stt import DeepgramSTTService

    return DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY", ""),
        settings=DeepgramSTTService.Settings(
            model=os.getenv("DEEPGRAM_MODEL", "nova-3"),
            language=os.getenv("STT_LANGUAGE", "multi"),
            smart_format=True,
            punctuate=True,
        ),
    )


def build_llm(system_instruction: str):
    """The brain. Cheap and fast beats clever here -- it is reading off a script.

    LLM_PROVIDER: openai (default) | xai | google | anthropic

    The system prompt goes on the service, not into LLMContext as a "system"
    message. That form is deprecated since Pipecat 1.9 and, worse, the Google
    and Anthropic adapters drop it silently -- you get a fluent assistant that
    has never heard of Fahrschule Infinity, with nothing in the logs.
    """
    provider = os.getenv("LLM_PROVIDER", "openai").lower()

    if provider == "google":
        from pipecat.services.google.llm import GoogleLLMService

        return GoogleLLMService(
            api_key=os.getenv("GOOGLE_API_KEY", ""),
            settings=GoogleLLMService.Settings(
                model=os.getenv("LLM_MODEL", "gemini-3.6-flash"),
                system_instruction=system_instruction,
            ),
        )

    if provider in ("xai", "grok"):
        # Grok speaks the OpenAI protocol, so GrokLLMService is a thin subclass
        # of the OpenAI one with x.ai's base URL. Non-reasoning by default:
        # reasoning latency is audible in a conversation.
        from pipecat.services.xai.llm import GrokLLMService

        return GrokLLMService(
            api_key=os.getenv("XAI_API_KEY", ""),
            settings=GrokLLMService.Settings(
                model=os.getenv("LLM_MODEL", "grok-4.20-non-reasoning"),
                system_instruction=system_instruction,
            ),
        )

    if provider == "anthropic":
        from pipecat.services.anthropic.llm import AnthropicLLMService

        return AnthropicLLMService(
            api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            settings=AnthropicLLMService.Settings(
                model=os.getenv("LLM_MODEL", "claude-haiku-4-5"),
                system_instruction=system_instruction,
            ),
        )

    if provider != "openai":
        # Previously any unrecognised value fell through to OpenAI, so a typo
        # like "gogle", or an LLM_PROVIDER line that accidentally swallowed a
        # second setting, produced a confusing "missing OpenAI key" instead of
        # naming the real mistake.
        raise ValueError(
            f"LLM_PROVIDER={provider!r} is not a provider. "
            "Use one of: openai, xai, google, anthropic. "
            "Check that the LLM_PROVIDER line in .env holds only the provider name."
        )

    from pipecat.services.openai.llm import OpenAILLMService

    return OpenAILLMService(
        api_key=os.getenv("OPENAI_API_KEY", ""),
        settings=OpenAILLMService.Settings(
            model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
            system_instruction=system_instruction,
        ),
    )


def build_tts():
    """The voice. This is what the owner will actually judge, so make it easy
    to A/B two providers on the same call flow.

    TTS_PROVIDER: cartesia (default, cheaper) | elevenlabs (more natural German)
    """
    provider = os.getenv("TTS_PROVIDER", "cartesia").lower()

    if provider == "elevenlabs":
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

        return ElevenLabsTTSService(
            api_key=os.getenv("ELEVENLABS_API_KEY", ""),
            settings=ElevenLabsTTSService.Settings(
                model=os.getenv("ELEVENLABS_MODEL", "eleven_flash_v2_5"),
                voice=os.getenv("ELEVENLABS_VOICE_ID", ""),
                language="de",
            ),
        )

    from pipecat.services.cartesia.tts import CartesiaTTSService

    return CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY", ""),
        settings=CartesiaTTSService.Settings(
            model=os.getenv("CARTESIA_MODEL", "sonic-2"),
            voice=os.getenv("CARTESIA_VOICE_ID", ""),
            language="de",
        ),
    )


def _save_transcript(context: LLMContext, session: CallSession) -> None:
    """Persist what was said, so the office can audit any call."""
    for message in context.get_messages():
        role = message.get("role")
        content = message.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            store.add_transcript_line(session.call_id, role, content.strip())


async def _end_call_after(seconds: int, runner: WorkerRunner, session: CallSession) -> None:
    """Hang up a call that has run too long, so a stuck line cannot drain credit."""
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        return  # Normal: the call ended on its own first.
    logger.warning(f"Call exceeded {seconds}s -- ending it to cap cost.")
    session.note(f"Automatisch beendet nach {seconds // 60} Minuten")
    await runner.cancel()


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments, session: CallSession):
    """Wire the pipeline and run one call to completion."""
    stt, llm, tts = build_stt(), build_llm(prompts.system_prompt()), build_tts()

    context = LLMContext(tools=TOOLS)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    is_phone = session.call_sid is not None
    rate = TELEPHONY_SAMPLE_RATE if is_phone else WEB_SAMPLE_RATE

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=rate,
            audio_out_sample_rate=rate,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        app_resources=session,
        idle_timeout_secs=runner_args.pipeline_idle_timeout_secs,
    )

    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint, force_gc=True)
    await runner.add_workers(worker)

    watchdog: asyncio.Task | None = None

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        nonlocal watchdog
        logger.info(f"Caller connected (call_sid={session.call_sid}, from={session.caller_id})")
        session.call_id = store.start_call(session.call_sid, session.caller_id)
        if MAX_CALL_SECONDS > 0:
            watchdog = asyncio.create_task(_end_call_after(MAX_CALL_SECONDS, runner, session))
        # Speak a fixed line and tell the context it was said, rather than
        # asking the model to compose it. See prompts.greeting_line().
        # No add_message here: the assistant aggregator already records what TTS
        # speaks, and adding it manually put the greeting in the context twice.
        await worker.queue_frames([TTSSpeakFrame(prompts.greeting_line())])

    @worker.event_handler("on_pipeline_error")
    async def on_pipeline_error(worker, frame):
        # A rate limit, an outage or a timeout otherwise reaches the caller as
        # dead air, and people hang up on silence.
        logger.error(f"Pipeline error: {frame.error}")
        session.note("Technischer Fehler im Gespraech")
        await worker.queue_frames([TTSSpeakFrame(prompts.FALLBACK_LINE)])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        if watchdog:
            watchdog.cancel()
        outcome = "; ".join(session.captured)
        logger.info(f"Call ended. Outcome: {outcome or 'no contact details captured'}")
        store.end_call(session.call_id, outcome)
        _save_transcript(context, session)
        await runner.cancel()

    try:
        await runner.run()
    finally:
        # Also covers the paths that never fire on_client_disconnected, so the
        # timer cannot outlive the call it was guarding.
        if watchdog and not watchdog.done():
            watchdog.cancel()


async def bot(runner_args: RunnerArguments):
    """Entry point the Pipecat runner calls for each incoming call."""
    store.init()

    call_data = runner_args.call_data
    session = CallSession(call_sid=call_data.call_id if call_data else None)
    if session.call_sid:
        session.caller_id = await telephony.caller_number(session.call_sid)

    # One set of params per transport we support; create_transport picks the
    # right one and builds the telephony serializer itself.
    transport_params = {
        "twilio": lambda: FastAPIWebsocketParams(audio_in_enabled=True, audio_out_enabled=True),
        "telnyx": lambda: FastAPIWebsocketParams(audio_in_enabled=True, audio_out_enabled=True),
        "webrtc": lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    }
    transport = await create_transport(runner_args, transport_params)

    await run_bot(transport, runner_args, session)


if __name__ == "__main__":
    # Registering the office dashboard on the runner's FastAPI app has to happen
    # before main() hands the app to uvicorn.
    from receptionist import dashboard  # noqa: F401

    missing = [k for k in ("DEEPGRAM_API_KEY",) if not os.getenv(k)]
    if missing:
        logger.error(f"Missing required env vars: {', '.join(missing)}. Copy .env.example to .env.")
        sys.exit(1)

    from pipecat.runner.run import main

    main()
