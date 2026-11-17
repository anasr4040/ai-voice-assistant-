"""Language switching: when the call changes language, and that it changes everywhere.

Run: uv run python tests/test_language.py

Free, no API keys. The last two sections run the real pipeline against the
local fake model, because the failure that matters here is a wiring one: the
model switching to English while the voice stays German, or the reverse.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "lang.db")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from loguru import logger  # noqa: E402

logger.remove()

from fake_llm_server import FakeLLM  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    Frame,
    TranscriptionFrame,
    TTSUpdateSettingsFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.worker import PipelineParams, PipelineWorker  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMContextAggregatorPair,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402
from pipecat.services.elevenlabs.tts_base import ELEVENLABS_V2_5_LANGUAGES  # noqa: E402
from pipecat.services.openai.llm import OpenAILLMService  # noqa: E402
from pipecat.transcriptions.language import Language  # noqa: E402
from pipecat.workers.runner import WorkerRunner  # noqa: E402

from receptionist import language as lang  # noqa: E402
from receptionist import prompts, store  # noqa: E402
from receptionist.tools import TOOLS, CallSession  # noqa: E402

PASSED, FAILED = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{f' -- {detail}' if detail else ''}")


def said(language: str, words: int) -> Counter:
    return Counter({language: words})


def policy_checks() -> None:
    print("\n1. When the call switches language, and when it must not")

    p = lang.LanguagePolicy()
    check("starts in German", p.current == "de")
    check("one stray 'yeah' does not switch", p.observe(said("en", 1)) is None)
    check("still German", p.current == "de")

    p = lang.LanguagePolicy()
    check(
        "'This is class B' (4 misheard words) does not switch on its own",
        p.observe(said("en", 4)) is None and p.current == "de",
    )
    p.observe(said("de", 3))
    check(
        "a German sentence in between cancels the pending switch",
        p.observe(said("en", 3)) is None and p.current == "de",
    )

    p = lang.LanguagePolicy()
    p.observe(said("en", 3))
    check("two short English utterances in a row do switch", p.observe(said("en", 3)) == "en")

    p = lang.LanguagePolicy()
    check(
        "one clear English sentence switches at once",
        p.observe(said("en", 7)) == "en" and p.current == "en",
    )
    check(
        "and a clear German sentence switches back",
        p.observe(said("de", 6)) == "de" and p.current == "de",
    )

    p = lang.LanguagePolicy()
    check(
        "a mostly German sentence with English words stays German",
        p.observe(Counter({"de": 6, "en": 5})) is None and p.current == "de",
    )
    check(
        "Turkish is followed",
        lang.LanguagePolicy().observe(said("tr", 6)) == "tr",
    )
    check(
        "Dutch (German phone audio is misdetected as it) is never switched to",
        lang.LanguagePolicy().observe(said("nl", 9)) is None,
    )

    p = lang.LanguagePolicy()
    check("an explicit request switches", p.choose("en") is True and p.current == "en")
    check(
        "and pins the language against auto-switching",
        p.observe(said("de", 8)) is None and p.current == "en",
    )
    check("asking for the current language changes nothing", p.choose("en") is False)


def detection_checks() -> None:
    print("\n2. Reading Deepgram's per-word languages")

    def word(text: str, language: str):
        return SimpleNamespace(word=text, language=language)

    result = SimpleNamespace(
        channel=SimpleNamespace(
            alternatives=[
                SimpleNamespace(
                    words=[
                        word("ich", "de"),
                        word("brauche", "de"),
                        word("den", "de"),
                        word("führerschein", "de"),
                        word("but", "en"),
                        word("i", "en"),
                        word("only", "en"),
                        word("speak", "en"),
                        word("english", "en"),
                    ]
                )
            ]
        )
    )
    frame = TranscriptionFrame(
        text="Ich brauche den Führerschein, but I only speak English",
        user_id="caller",
        timestamp="",
        language=Language.DE,  # utterance label says German...
        result=result,
    )
    counts = lang.word_languages(frame)
    check(
        "counts words per language, not the utterance label",
        counts == Counter({"en": 5, "de": 4}),
        str(dict(counts)),
    )

    plain = TranscriptionFrame(
        text="hello there how are you", user_id="caller", timestamp="", language=Language.EN_US
    )
    check(
        "falls back to the frame's language without word tags",
        lang.word_languages(plain) == Counter({"en": 5}),
    )

    for raw, expected in [
        ("en", "en"),
        ("en-US", "en"),
        ("English", "en"),
        ("Englisch", "en"),
        ("Türkisch", "tr"),
        ("turkish", "tr"),
        ("AR", "ar"),
        ("klingon", None),
        ("nl", None),
    ]:
        check(f"resolve({raw!r}) -> {expected}", lang.resolve(raw) == expected)


def coverage_checks() -> None:
    print("\n3. Every supported language can actually be spoken")
    for code in lang.SUPPORTED:
        try:
            Language(code)
            known = True
        except ValueError:
            known = False
        check(f"{code}: Pipecat knows it", known)
        check(f"{code}: ElevenLabs flash v2.5 speaks it", code in ELEVENLABS_V2_5_LANGUAGES)
        missing = [k for k, lines in lang.PHRASES.items() if not lines.get(code)]
        check(f"{code}: every fixed line is translated", not missing, ", ".join(missing))


def prompt_checks() -> None:
    print("\n4. The prompt tells the model the language instead of asking it to guess")
    german = prompts.system_prompt("de")
    english = prompts.system_prompt("en")
    turkish = prompts.system_prompt("tr")
    check("German call: answer in German", "Antworte ausschließlich auf Deutsch" in german)
    check("English call: answer in English", "Antworte ausschließlich auf Englisch" in english)
    check("Turkish call: answer in Turkish", "Antworte ausschließlich auf Türkisch" in turkish)
    check("other languages get the translation note", "sinngemäß auf Türkisch" in turkish)
    check("German call has no translation note", "sinngemäß auf" not in german)
    check("explains when to call switch_language", "switch_language" in german)
    check("still: a single word never switches", "Ein einzelnes Wort wechselt NIE" in german)
    check("tells the model to write real umlauts", 'nie "Fuehrerschein"' in german)
    check("no transliterated umlauts left to read aloud", "Rueckruf" not in german)


class Recorder(FrameProcessor):
    """End of the test pipeline: keeps every frame that made it through."""

    def __init__(self):
        super().__init__()
        self.frames: list[Frame] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        self.frames.append(frame)
        await self.push_frame(frame, direction)

    def tts_languages(self) -> list[str]:
        out = []
        for frame in self.frames:
            if isinstance(frame, TTSUpdateSettingsFrame):
                value = getattr(frame.delta, "language", None) if frame.delta else None
                out.append(lang.normalize(value) or "")
        return out


async def run_call(turns: list[dict], transcript: TranscriptionFrame, expect_requests: int):
    """Real switcher -> real aggregator -> real OpenAI service -> recorder."""
    fake = FakeLLM(turns)
    base_url = await fake.start()
    session = CallSession(call_sid=None, caller_id="+4917612345678")
    session.call_id = store.start_call(None, "+4917612345678")

    llm = OpenAILLMService(
        api_key="fake-key-for-local-server",
        base_url=base_url,
        settings=OpenAILLMService.Settings(
            model="fake-model", system_instruction=prompts.system_prompt("de")
        ),
    )
    context = LLMContext(tools=TOOLS)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)
    recorder = Recorder()
    pipeline = Pipeline(
        [
            lang.LanguageSwitcher(session.language),
            user_aggregator,
            llm,
            recorder,
            assistant_aggregator,
        ]
    )
    worker = PipelineWorker(pipeline, params=PipelineParams(), app_resources=session)
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    task = asyncio.create_task(runner.run())
    try:
        await asyncio.sleep(0.3)
        await worker.queue_frames([transcript])
        for _ in range(100):
            if len(fake.requests) >= expect_requests:
                await asyncio.sleep(0.5)
                break
            await asyncio.sleep(0.1)
    finally:
        await runner.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(task, timeout=5)
        await fake.stop()
    return fake, recorder, session


def prompt_of(request: dict) -> str:
    for message in request.get("messages", []):
        if message.get("role") in ("system", "developer"):
            return str(message.get("content", ""))
    return ""


async def pipeline_checks() -> None:
    print("\n5. A caller who speaks English moves the model AND the voice")
    english = TranscriptionFrame(
        text="Hello, I would like to get a driving licence please",
        user_id="caller",
        timestamp="2026-11-17T10:00:00",
        language=Language.EN,
    )
    fake, recorder, session = await run_call(
        [{"text": "Of course. Are you thinking of a car licence?"}], english, 1
    )
    check("model was called", len(fake.requests) >= 1)
    if fake.requests:
        check(
            "the model's prompt already said English for this very answer",
            "Antworte ausschließlich auf Englisch" in prompt_of(fake.requests[0]),
        )
    check(
        "the voice was switched to English",
        recorder.tts_languages() == ["en"],
        str(recorder.tts_languages()),
    )
    check("the call's language is recorded", session.language.current == "en")

    print("\n6. 'Can we speak Turkish?' -- the model asks, the tool switches")
    german_request = TranscriptionFrame(
        text="Können wir Türkisch sprechen?",
        user_id="caller",
        timestamp="2026-11-17T10:00:00",
        language=Language.DE,
    )
    fake, recorder, session = await run_call(
        [
            {"tool": "switch_language", "args": {"language": "tr"}},
            {"text": "Tabii, nasıl yardımcı olabilirim?"},
        ],
        german_request,
        2,
    )
    check("model was called again after the tool", len(fake.requests) >= 2)
    if len(fake.requests) >= 2:
        check(
            "first answer was still under the German prompt",
            "Antworte ausschließlich auf Deutsch" in prompt_of(fake.requests[0]),
        )
        check(
            "the answer after the tool is under the Turkish prompt",
            "Antworte ausschließlich auf Türkisch" in prompt_of(fake.requests[1]),
        )
    check(
        "the voice was switched to Turkish",
        recorder.tts_languages() == ["tr"],
        str(recorder.tts_languages()),
    )
    check("and the choice is pinned", session.language.pinned)


async def main() -> int:
    store.init()
    policy_checks()
    detection_checks()
    coverage_checks()
    prompt_checks()
    await pipeline_checks()

    print(f"\n{'=' * 58}\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("FAILED: " + ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
