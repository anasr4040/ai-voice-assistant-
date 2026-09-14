"""Exercise every tool the receptionist can call, without any API keys.

Run: uv run python tests/test_tools.py
This is the cheap check. It proves the booking, lead-capture and transfer logic
before you spend a cent on STT, LLM or TTS.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "test.db")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from receptionist import config, store, tools  # noqa: E402
from receptionist.tools import CallSession  # noqa: E402

PASSED, FAILED, TODOS = [], [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    """A hard assertion about behaviour. Failing means the code is wrong."""
    (PASSED if condition else FAILED).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{f' -- {detail}' if detail else ''}")


def todo(name: str, condition: bool, detail: str = "") -> None:
    """A config value only the owner can supply. Missing is not a code failure."""
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        TODOS.append(f"{name} -- {detail}")
        print(f"  TODO  {name} -- {detail}")


class FakeParams:
    """Stands in for Pipecat's FunctionCallParams: captures the tool's result."""

    def __init__(self, session: CallSession):
        self.app_resources = session
        self.result: dict | None = None
        self.llm = self
        self.frames: list = []

    async def result_callback(self, result):
        self.result = result

    async def push_frame(self, frame):
        self.frames.append(frame)


async def main() -> int:
    store.init()
    session = CallSession(call_sid=None, caller_id="+4917612345678")

    print("\n1. Caller asks when they could come in")
    params = FakeParams(session)
    await tools.check_available_appointments(params)
    slots = params.result.get("slots", [])
    check("offers free slots", bool(slots), f"{len(slots)} offered")
    check("offers at most 3", len(slots) <= 3)
    check(
        "slots are speakable",
        all("spoken" in s for s in slots),
        slots[0]["spoken"] if slots else "",
    )

    print("\n2. Caller asks for a day we cannot parse")
    params = FakeParams(session)
    await tools.check_available_appointments(params, day="irgendwann mal")
    check("asks again instead of guessing", params.result.get("understood") is False)

    print("\n3. Caller books a real slot")
    first = slots[0]
    params = FakeParams(session)
    await tools.book_appointment(
        params,
        name="Anna Schmidt",
        phone="+4917612345678",
        day=first["date"],
        time=first["time"],
    )
    check("booking accepted", params.result.get("booked") is True, str(params.result.get("date")))
    check("row written", any(b["name"] == "Anna Schmidt" for b in store.recent_bookings()))

    print("\n4. Someone else tries the same slot")
    params = FakeParams(session)
    await tools.book_appointment(
        params,
        name="Ben Meier",
        phone="+49170000",
        day=first["date"],
        time=first["time"],
    )
    check("double booking refused", params.result.get("booked") is False)
    check("alternatives offered", bool(params.result.get("alternatives")))

    print("\n5. Caller invents a time we never offer")
    params = FakeParams(session)
    await tools.book_appointment(
        params,
        name="Carla Weiss",
        phone="+49170111",
        day=first["date"],
        time="03:00",
    )
    check("refuses unoffered slot", params.result.get("booked") is False)

    print("\n6. Caller wants a callback")
    params = FakeParams(session)
    await tools.take_callback_request(
        params,
        name="Dirk Klein",
        phone="",
        topic="Frage zur MPU",
    )
    check("saved using caller ID when no number given", params.result.get("saved") is True)
    lead = store.recent_leads()[0]
    check("used the caller's own number", lead["phone"] == "+4917612345678", lead["phone"])

    print("\n7. Caller demands a human, no transfer number configured")
    saved, config.TRANSFER_NUMBER = config.TRANSFER_NUMBER, ""
    params = FakeParams(session)
    await tools.transfer_to_staff(params, reason="Beschwerde")
    check("falls back instead of dead air", params.result.get("transferred") is False)
    check("tells the model to take a callback", "callback" in params.result.get("say", "").lower())
    config.TRANSFER_NUMBER = saved

    print("\n8. Config sanity")
    for day, times in config.BOOKABLE["slots"].items():
        hours = config.OPENING_HOURS.get(day)
        check(
            f"{day}: bookable slots fall inside opening hours",
            bool(hours) and all(hours[0] <= t < hours[1] for t in times),
        )
    todo(
        "transfer number configured",
        bool(config.TRANSFER_NUMBER),
        "set TRANSFER_NUMBER in .env, or transfers fall back to callbacks",
    )
    todo(
        "real address configured",
        "Musterstrasse" not in config.BUSINESS["address"],
        "config.py still has the placeholder address",
    )
    todo(
        "real prices configured",
        "420 Euro" not in config.PRICES["grundbetrag"],
        "config.py still has placeholder prices -- the bot will quote invented numbers",
    )

    print(f"\n{'=' * 58}")
    print(f"{len(PASSED)} passed, {len(FAILED)} failed, {len(TODOS)} awaiting real data")
    if FAILED:
        print("\nFAILED (code bugs): " + ", ".join(FAILED))
    if TODOS:
        print("\nBefore the owner hears this:")
        for item in TODOS:
            print(f"  - {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
