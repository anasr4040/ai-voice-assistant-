"""End-to-end test of the real pipeline, at zero API cost.

Everything here is production code except the model itself: the real
OpenAILLMService, the real adapter, Pipecat's real tool-call parsing and
dispatch, the real tools, and a real SQLite database. Only the model's
judgement is scripted, by pointing the service at a local server that speaks
OpenAI's streaming protocol.

This is the test that answers "will a real call work", which unit tests on the
tools cannot: it catches pipeline wiring, tool registration, schema conversion
and result round-tripping.

Run: uv run python tests/test_pipeline_e2e.py
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "e2e.db")
os.environ.setdefault("OPENAI_API_KEY", "fake-key-for-local-server")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from loguru import logger  # noqa: E402

logger.remove()  # the services log the whole prompt at DEBUG

from fake_llm_server import FakeLLM  # noqa: E402
from pipecat.frames.frames import LLMRunFrame, TranscriptionFrame  # noqa: E402
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.worker import PipelineParams, PipelineWorker  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMContextAggregatorPair,
)
from pipecat.services.openai.llm import OpenAILLMService  # noqa: E402
from pipecat.workers.runner import WorkerRunner  # noqa: E402

from receptionist import prompts, store  # noqa: E402
from receptionist import tools as tools_module  # noqa: E402
from receptionist.tools import TOOLS, CallSession  # noqa: E402

PASSED, FAILED = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{f' -- {detail}' if detail else ''}")


async def drive(turns: list[dict], said: str, expect_requests: int = 1) -> FakeLLM:
    """Run one scripted exchange through the real pipeline, as production does.

    Uses PipelineWorker and WorkerRunner exactly like bot.py, rather than a
    synchronous test harness: the model call is asynchronous, so the test has
    to actually wait for it the way a real call would.

    Args:
        turns: What the fake model returns, one entry per request.
        said: What the caller says, as speech-to-text would deliver it.
        expect_requests: Wait until the model has been called this many times.
    """
    fake = FakeLLM(turns)
    base_url = await fake.start()

    llm = OpenAILLMService(
        api_key="fake-key-for-local-server",
        base_url=base_url,
        settings=OpenAILLMService.Settings(
            model="fake-model",
            system_instruction=prompts.system_prompt(),
        ),
    )

    context = LLMContext(tools=TOOLS)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)
    session = CallSession(call_sid=None, caller_id="+4917612345678")
    session.call_id = store.start_call(None, "+4917612345678")

    pipeline = Pipeline([user_aggregator, llm, assistant_aggregator])
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=False),
        app_resources=session,
    )
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    run_task = asyncio.create_task(runner.run())

    try:
        await asyncio.sleep(0.3)  # let StartFrame propagate
        await worker.queue_frames(
            [
                TranscriptionFrame(text=said, user_id="caller", timestamp="2026-09-17T10:00:00"),
                LLMRunFrame(),
            ]
        )
        # Wait for the model to be called the expected number of times.
        for _ in range(100):
            if len(fake.requests) >= expect_requests:
                await asyncio.sleep(0.4)  # let the tool run and the result post back
                break
            await asyncio.sleep(0.1)
    finally:
        await runner.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(run_task, timeout=5)
        await fake.stop()
    return fake


async def main() -> int:
    store.init()

    print("\n1. The pipeline reaches the model with the right payload")
    fake = await drive(
        [{"text": "Guten Tag, Fahrschule Infinity, mein Name ist Mia."}],
        "Guten Tag, ich habe eine Frage.",
    )
    check("model was actually called", len(fake.requests) >= 1, f"{len(fake.requests)} request(s)")
    offered = fake.tools_offered()
    check(
        "all four tools were advertised",
        set(offered)
        == {
            "check_available_appointments",
            "book_appointment",
            "take_callback_request",
            "transfer_to_staff",
        },
        ", ".join(sorted(offered)),
    )
    prompt = fake.system_prompt()
    check("system prompt arrived", "Fahrschule Infinity" in prompt, f"{len(prompt)} chars")
    check("price refusal is in the prompt the model sees", "Preise NICHT" in prompt)
    check(
        "caller's words reached the model",
        any("eine Frage" in str(r.get("messages")) for r in fake.requests),
    )

    print("\n2. A tool call from the model actually executes")
    before = len(store.recent_leads())
    fake = await drive(
        [
            {
                "tool": "take_callback_request",
                "args": {"name": "Anna Schmidt", "phone": "", "topic": "Frage zur Klasse B"},
            },
            {"text": "Ein Kollege meldet sich bei Ihnen."},
        ],
        "Kann mich jemand zurueckrufen?",
        expect_requests=2,
    )
    leads = store.recent_leads()
    check("lead was written to the database", len(leads) == before + 1)
    if leads:
        check("name captured", leads[0]["name"] == "Anna Schmidt", leads[0]["name"])
        check(
            "empty phone fell back to the caller ID",
            leads[0]["phone"] == "+4917612345678",
            leads[0]["phone"],
        )
    check("tool result was fed back to the model", fake.sent_tool_result())
    check(
        "model was called again after the tool",
        len(fake.requests) >= 2,
        f"{len(fake.requests)} requests",
    )

    print("\n3. A booking round-trips through the real dispatch")
    slot = tools_module._describe_slot(store.free_slots()[0])
    fake = await drive(
        [
            {
                "tool": "book_appointment",
                "args": {
                    "name": "Ben Meier",
                    "phone": "+4915112345",
                    "email": "ben punkt meier at web punkt de",
                    "slot_id": slot["slot_id"],
                    "location": "Barmbek",
                },
            },
            {"text": "Ihr Termin steht."},
        ],
        "Ich moechte einen Beratungstermin.",
        expect_requests=2,
    )
    bookings = [b for b in store.recent_bookings() if b["name"] == "Ben Meier"]
    check("booking was written", len(bookings) == 1)
    if bookings:
        check("branch recorded", bookings[0]["location"] == "Barmbek", str(bookings[0]["location"]))
        check(
            "spoken email normalised through the real dispatch",
            bookings[0]["email"] == "ben.meier@web.de",
            str(bookings[0]["email"]),
        )
        check(
            "slot matches the id that was offered",
            bookings[0]["slot_date"] in slot["slot_id"],
            slot["slot_id"],
        )

    print("\n4. A bad tool call is refused, not crashed on")
    fake = await drive(
        [
            {
                "tool": "book_appointment",
                "args": {
                    "name": "Carla",
                    "phone": "+49170",
                    "email": "",
                    "slot_id": slot["slot_id"],
                    "location": "Altona",  # not a real branch
                },
            },
            {"text": "Welche Filiale darf es sein?"},
        ],
        "Termin in Altona bitte.",
        expect_requests=2,
    )
    check(
        "no booking for an invented branch",
        not [b for b in store.recent_bookings() if b["name"] == "Carla"],
    )
    check("pipeline survived and asked again", len(fake.requests) >= 2)

    print(f"\n{'=' * 58}\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("FAILED: " + ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
