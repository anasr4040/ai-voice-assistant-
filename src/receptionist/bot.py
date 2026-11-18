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
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    EndWorkerFrame,
    LLMMessagesAppendFrame,
    TTSSpeakFrame,
    UserIdleTimeoutUpdateFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from receptionist import config, language, prompts, store, telephony, tools
from receptionist.dictation import (
    DictationAwareMinWords,
    HeardRecorder,
    ShortReplyUserTurnStopStrategy,
)
from receptionist.language import LanguageSwitcher, phrase
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

# Words the caller must say before the bot stops talking. At 1, every "mhm",
# "ja" or cough cut the bot off mid-sentence -- the single most robotic thing a
# phone bot does, because people make those noises to show they ARE listening.
# When the bot is silent, one word is still enough to start the caller's turn.
INTERRUPT_MIN_WORDS = int(os.getenv("INTERRUPT_MIN_WORDS", "2"))

# Seconds of silence after the bot finishes before it checks the caller is
# still there. Twice it asks; the third time it says goodbye and hangs up, so
# a dropped call does not sit open billing by the minute. 0 disables.
USER_IDLE_SECONDS = float(os.getenv("USER_IDLE_SECONDS", "10"))
IDLE_PROMPTS_BEFORE_HANGUP = 2

# Which speech-to-text listens, and so who decides when the caller is done.
#   flux: Deepgram Flux. Speech recognition and end-of-turn detection in one
#         model, and it starts the reply while the caller is still finishing.
#         The fastest turn-taking, but it hears 7 of Mia's 12 languages
#         (no Turkish, Arabic, Ukrainian, Polish or Romanian).
#   nova: Deepgram nova-3 plus the local Smart Turn model. All 12 languages.
STT_ENGINE = os.getenv("STT_ENGINE", "flux").strip().lower()

# Flux: languages to bias recognition toward. Most callers speak German, the
# rest mostly English. Empty means detect freely among Flux's languages.
FLUX_LANGUAGE_HINTS = [
    code.strip() for code in os.getenv("FLUX_LANGUAGE_HINTS", "de,en").split(",") if code.strip()
]

# Flux: answer Flux's early "probably finished" signal, discarding the reply
# if the caller keeps talking. Saves most of the wait before each answer.
FLUX_EAGER = os.getenv("FLUX_EAGER", "1") != "0"

# nova only: longest silence before the caller's turn is treated as over when
# the turn model is unsure. In a test call this wait was most of the gap
# before Mia answered. Pauses while dictating get a "mhm" either way.
TURN_STOP_SECS = float(os.getenv("TURN_STOP_SECS", "1.0"))

# A technical error is announced at most this often. More than that and the
# caller hears the same apology on a loop while nothing else happens.
MAX_FALLBACK_LINES = 2


def stt_keyterms() -> list[str]:
    """Words speech-to-text should expect: our names, branches, licence classes.

    Proper names are where phone-quality speech-to-text slips most: a branch
    that arrives as "Bambeck" or "Bill Stadt" cannot be matched to a booking.
    """
    terms = [config.BUSINESS["name"], config.ASSISTANT_NAME, *config.LOCATIONS]
    terms += [f"Klasse {k}" for k in config.LICENCE_CLASSES]
    terms += [
        "Führerschein",
        "Beratungsgespräch",
        "Fahrstunde",
        "Theorieunterricht",
        "Erste-Hilfe-Kurs",
        "Sehtest",
        "Begleitetes Fahren",
        "Landesbetrieb Verkehr",
    ]
    return terms


def build_stt():
    """Deepgram, configured for a German caller who may switch language.

    STT_ENGINE picks between Flux (fastest turn-taking, 7 languages) and
    nova-3 (all 12 languages, turn-taking by the local Smart Turn model). Both
    handle switching language inside one call.
    """
    use_keyterms = os.getenv("STT_KEYTERMS", "1") != "0"
    keyterms = stt_keyterms() if use_keyterms else None

    if STT_ENGINE == "flux":
        from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService

        return DeepgramFluxSTTService(
            api_key=os.getenv("DEEPGRAM_API_KEY", ""),
            settings=DeepgramFluxSTTService.Settings(
                model=os.getenv("DEEPGRAM_FLUX_MODEL", "flux-general-multi"),
                language_hints=[Language(code) for code in FLUX_LANGUAGE_HINTS] or None,
                keyterm=keyterms or [],
            ),
            enable_eager_end_of_turn=FLUX_EAGER,
        )

    if STT_ENGINE != "nova":
        raise ValueError(f"STT_ENGINE={STT_ENGINE!r} is not an engine. Use flux or nova.")

    from pipecat.services.deepgram.stt import DeepgramSTTService

    return DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY", ""),
        settings=DeepgramSTTService.Settings(
            model=os.getenv("DEEPGRAM_MODEL", "nova-3"),
            language=os.getenv("STT_LANGUAGE", "multi"),
            smart_format=True,
            punctuate=True,
            # Keyterm prompting is a nova-3 feature; set STT_KEYTERMS=0 if you
            # switch to a model that rejects it.
            keyterm=keyterms,
        ),
    )


def build_user_params(session: CallSession) -> LLMUserAggregatorParams:
    """How the caller's turns are detected, for the engine in use."""
    if STT_ENGINE == "flux":
        # Flux decides when a turn starts and ends, and Pipecat applies the
        # turn strategies Flux recommends when none are given here. Adding a
        # local detector would trigger a second reply for the same turn.
        return LLMUserAggregatorParams(user_idle_timeout=USER_IDLE_SECONDS)

    return LLMUserAggregatorParams(
        vad_analyzer=SileroVADAnalyzer(),
        user_turn_strategies=UserTurnStrategies(
            # Two words to interrupt, so "mhm" does not cut the bot off --
            # dropping to one while the caller dictates, so no digit of a
            # number is thrown away.
            start=[DictationAwareMinWords(session=session, min_words=INTERRUPT_MIN_WORDS)],
            stop=[
                # "Yes", "danke", "bye": over the moment they are said.
                ShortReplyUserTurnStopStrategy(session=session),
                # Everything else: Smart Turn decides, waiting at most
                # TURN_STOP_SECS when it is unsure.
                TurnAnalyzerUserTurnStopStrategy(
                    turn_analyzer=LocalSmartTurnAnalyzerV3(
                        params=SmartTurnParams(stop_secs=TURN_STOP_SECS)
                    )
                ),
            ],
        ),
        user_idle_timeout=USER_IDLE_SECONDS,
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

    # Lower is steadier: the same question gets the same answer, and the model
    # strays less from the script. Opt-in, because reasoning models (the gpt-5
    # family, for one) reject the parameter outright.
    raw_temperature = os.getenv("LLM_TEMPERATURE", "").strip()
    temperature = float(raw_temperature) if raw_temperature else None

    if provider == "google":
        from pipecat.services.google.llm import GoogleLLMService

        return GoogleLLMService(
            api_key=os.getenv("GOOGLE_API_KEY", ""),
            settings=GoogleLLMService.Settings(
                model=os.getenv("LLM_MODEL", "gemini-3.6-flash"),
                system_instruction=system_instruction,
                **({"temperature": temperature} if temperature is not None else {}),
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
                **({"temperature": temperature} if temperature is not None else {}),
            ),
        )

    if provider == "anthropic":
        from pipecat.services.anthropic.llm import AnthropicLLMService

        return AnthropicLLMService(
            api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            settings=AnthropicLLMService.Settings(
                model=os.getenv("LLM_MODEL", "claude-haiku-4-5"),
                system_instruction=system_instruction,
                **({"temperature": temperature} if temperature is not None else {}),
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
            # Fast and non-reasoning: a reasoning model "thinks" before every
            # reply, and on a phone that pause is audible.
            model=os.getenv("LLM_MODEL", "gpt-4.1-mini"),
            system_instruction=system_instruction,
            **({"temperature": temperature} if temperature is not None else {}),
        ),
    )


def build_tts(language: str = "de"):
    """The voice. This is what the owner will actually judge, so make it easy
    to A/B two providers on the same call flow.

    TTS_PROVIDER: cartesia (default, cheaper) | elevenlabs (more natural German)

    `language` is only the starting point: receptionist.language switches it
    mid-call together with the model, so English is pronounced as English.
    """
    provider = os.getenv("TTS_PROVIDER", "cartesia").lower()

    if provider == "elevenlabs":
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

        return ElevenLabsTTSService(
            api_key=os.getenv("ELEVENLABS_API_KEY", ""),
            settings=ElevenLabsTTSService.Settings(
                model=os.getenv("ELEVENLABS_MODEL", "eleven_flash_v2_5"),
                voice=os.getenv("ELEVENLABS_VOICE_ID", ""),
                language=language,
                # Default delivery is too quick and too bright for someone who
                # is nervous about a driving licence and may not be a native
                # speaker. Slower and steadier reads as calm rather than rushed.
                # Range is 0.7 to 1.2; below about 0.85 starts to sound sedated.
                speed=float(os.getenv("TTS_SPEED", "0.92")),
                # Higher stability is a steadier, less theatrical read.
                stability=float(os.getenv("TTS_STABILITY", "0.6")),
                similarity_boost=float(os.getenv("TTS_SIMILARITY", "0.75")),
                # Style is drama. A receptionist does not need any.
                style=float(os.getenv("TTS_STYLE", "0.0")),
                use_speaker_boost=True,
            ),
        )

    from pipecat.services.cartesia.tts import CartesiaTTSService

    return CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY", ""),
        settings=CartesiaTTSService.Settings(
            model=os.getenv("CARTESIA_MODEL", "sonic-2"),
            voice=os.getenv("CARTESIA_VOICE_ID", ""),
            language=language,
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
    session.event(f"Automatisch beendet nach {seconds // 60} Minuten")
    await runner.cancel()


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments, session: CallSession):
    """Wire the pipeline and run one call to completion."""
    session.idle_seconds = USER_IDLE_SECONDS
    if STT_ENGINE == "flux":
        language.set_available(language.FLUX_LANGUAGES)
    current = session.language.current
    stt = build_stt()
    llm = build_llm(prompts.system_prompt(current))
    tts = build_tts(current)

    context = LLMContext(tools=TOOLS)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=build_user_params(session),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            # Right after speech-to-text: decides the call's language and moves
            # the model and the voice before they handle what was just said.
            LanguageSwitcher(session.language),
            # Remembers what the caller actually said, so a tool can refuse a
            # number or address the model made up.
            HeardRecorder(session),
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
    idle_prompts = 0
    fallback_lines = 0

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
        # dead air, and people hang up on silence. Fixed text, in the call's
        # language, because the model may be exactly what just failed.
        nonlocal fallback_lines
        logger.error(f"Pipeline error: {frame.error}")
        session.event("Technischer Fehler im Gespräch")
        if fallback_lines < MAX_FALLBACK_LINES:
            fallback_lines += 1
            await worker.queue_frames([TTSSpeakFrame(phrase("fallback", session.language.current))])

    @user_aggregator.event_handler("on_user_turn_started")
    async def on_user_turn_started(aggregator, strategy):
        nonlocal idle_prompts
        idle_prompts = 0

    @user_aggregator.event_handler("on_user_turn_idle")
    async def on_user_turn_idle(aggregator):
        # A person on the phone does not sit in silence forever, and does not
        # hang up the moment the other side goes quiet either. Check in --
        # worded by the model, so it is in the right language and fits what
        # was just said -- and only give up after that has not worked twice.
        nonlocal idle_prompts
        if session.transferring:
            return
        if session.dictating:
            # Silence after a "mhm" means the caller has finished dictating
            # and is waiting to hear it back. Not an idle caller. Read it back
            # straight away from code -- asking the model first cost a second.
            tool = "read_back_phone_number" if session.dictating == "phone" else "read_back_email"
            back_to_normal = UserIdleTimeoutUpdateFrame(timeout=session.idle_seconds)
            line = tools.finish_dictation(session)
            if line:
                await worker.queue_frames([back_to_normal, TTSSpeakFrame(line)])
                return
            await worker.queue_frames(
                [
                    back_to_normal,
                    LLMMessagesAppendFrame(
                        messages=[
                            {
                                "role": "developer",
                                "content": (
                                    "(Hinweis vom System: Der Anrufer hat aufgehört zu "
                                    f"diktieren. Rufe jetzt {tool} mit allem, was er davon "
                                    "gesagt hat, und finished=true auf.)"
                                ),
                            }
                        ],
                        run_llm=True,
                    ),
                ]
            )
            return
        idle_prompts += 1
        if idle_prompts > IDLE_PROMPTS_BEFORE_HANGUP:
            session.event("Aufgelegt nach Stille")
            await worker.queue_frames(
                [
                    TTSSpeakFrame(phrase("goodbye_idle", session.language.current)),
                    EndWorkerFrame(),
                ]
            )
            return
        hint = (
            "Kurz und freundlich fragen, ob der Anrufer noch da ist."
            if idle_prompts == 1
            else "Sagen, dass er sich ruhig Zeit lassen kann, du bist noch da."
        )
        await worker.queue_frames(
            [
                LLMMessagesAppendFrame(
                    messages=[
                        {
                            "role": "developer",
                            "content": (
                                f"(Hinweis vom System: Der Anrufer sagt seit "
                                f"{int(USER_IDLE_SECONDS)} Sekunden nichts. {hint} "
                                "Ein Satz, in der Sprache des Gesprächs.)"
                            ),
                        }
                    ],
                    run_llm=True,
                )
            ]
        )

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        if watchdog:
            watchdog.cancel()
        outcome = session.outcome
        logger.info(f"Call ended. Outcome: {outcome or 'no contact details captured'}")
        store.end_call(
            session.call_id,
            outcome,
            captured=bool(session.captured),
            language=session.language.current,
        )
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
