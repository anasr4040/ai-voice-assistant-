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

import re  # noqa: E402

from receptionist import config, store, tools  # noqa: E402
from receptionist.prompts import system_prompt  # noqa: E402
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
        all("say_german" in s and "say_english" in s for s in slots),
        slots[0]["say_german"] if slots else "",
    )

    print("\n2. Caller asks for a day we cannot parse")
    params = FakeParams(session)
    await tools.check_available_appointments(params, day="irgendwann mal")
    check("offers slots instead of dead-ending", bool(params.result.get("slots")))

    print("\n3. Caller books a real slot")
    first = slots[0]
    params = FakeParams(session)
    await tools.book_appointment(
        params,
        name="Anna Schmidt",
        phone="+4917612345678",
        slot_id=first["slot_id"],
        location="Barmbek",
    )
    check("booking accepted", params.result.get("booked") is True, first["slot_id"])
    check("branch recorded in the tool result", params.result.get("location") == "Barmbek")
    # The tool's return value said "Barmbek" while the database column stayed
    # NULL, because add_booking dropped the field. Assert the stored row.
    stored = [b for b in store.recent_bookings() if b["name"] == "Anna Schmidt"]
    check(
        "branch persisted to the database",
        bool(stored) and stored[0]["location"] == "Barmbek",
        str(stored[0]["location"]) if stored else "no row",
    )
    check(
        "confirmation names the branch address",
        "Bramfelder" in params.result.get("say", ""),
    )
    check("row written", any(b["name"] == "Anna Schmidt" for b in store.recent_bookings()))

    print("\n4. Someone else tries the same slot")
    params = FakeParams(session)
    await tools.book_appointment(
        params,
        name="Ben Meier",
        phone="+49170000",
        slot_id=first["slot_id"],
        location="Barmbek",
    )
    check("double booking refused", params.result.get("booked") is False)
    check("alternatives offered", bool(params.result.get("alternatives")))

    print("\n5. Caller invents a time we never offer")
    params = FakeParams(session)
    await tools.book_appointment(
        params,
        name="Carla Weiss",
        phone="+49170111",
        slot_id=first["slot_id"].replace("T16:00", "T03:00"),
        location="Barmbek",
    )
    check("refuses unoffered slot", params.result.get("booked") is False)

    print("\n5b. Caller names a branch that does not exist")
    params = FakeParams(session)
    await tools.book_appointment(
        params,
        name="Emil Braun",
        phone="+49170222",
        slot_id=first["slot_id"],
        location="Altona",
    )
    check("refuses unknown branch", params.result.get("booked") is False)
    check("offers the four real branches", len(params.result.get("locations", [])) == 4)

    print("\n5c. Branch with no street on file is still bookable")
    free = tools._describe_slot(store.free_slots()[0])
    params = FakeParams(session)
    await tools.book_appointment(
        params,
        name="Fatima Yilmaz",
        phone="+49170333",
        slot_id=free["slot_id"],
        location="Billstedt",
    )
    say = params.result.get("say", "")
    check("books without a known street", params.result.get("booked") is True)
    check("never invents a street", "strasse" not in say.lower().replace("hannoversche", ""))
    check("promises the address follows", "SMS" in say)

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

    print("\n8. Regressions (each of these was a real bug)")

    # end_call matched on call_sid; browser calls have none, so every past
    # browser call got rewritten with the newest outcome.
    a = store.start_call(None, "web1")
    store.end_call(a, "Termin gebucht")
    b = store.start_call(None, "web2")
    store.end_call(b, "")
    c = store.start_call(None, "web3")
    store.end_call(c, "Rueckruf notiert")
    stats = store.call_stats()
    check(
        "calls without a call_sid stay separate",
        (stats["total"], stats["captured"]) == (3, 2),
        f"total={stats['total']} captured={stats['captured']}",
    )

    # "+49 40 64421700" is not E.164; Twilio <Dial> drops the call.
    check(
        "transfer number is dialable E.164",
        bool(re.fullmatch(r"\+[1-9]\d{1,14}", config.TRANSFER_NUMBER)),
        config.TRANSFER_NUMBER,
    )
    check(
        "normalize_phone strips spaces",
        config.normalize_phone("+49 40 644 217 00") == "+494064421700",
    )
    check(
        "normalize_phone handles 00 prefix",
        config.normalize_phone("0049 40 64421700") == "+494064421700",
    )

    # A malformed time took down the whole office dashboard.
    check("speak_time survives junk", config.speak_time("sechzehn Uhr") == "sechzehn Uhr")

    print("\n8b. Times the model actually sends")
    # "16 Uhr" is the most natural thing a German-speaking model emits. It used
    # to be rejected, and the caller was told a free slot was taken.
    for spoken, expected in [
        ("16:00", "16:00"),
        ("16", "16:00"),
        ("16 Uhr", "16:00"),
        ("sechzehn Uhr", "16:00"),
        ("1600", "16:00"),
        ("16 Uhr 30", "16:30"),
        ("18h", "18:00"),
    ]:
        check(
            f"parses {spoken!r}",
            store.parse_spoken_time(spoken) == expected,
            str(store.parse_spoken_time(spoken)),
        )
    for junk in ["", "quatsch", "25:00", "16:99", "halb vier"]:
        check(f"refuses {junk!r} rather than guessing", store.parse_spoken_time(junk) is None)

    free = tools._describe_slot(store.free_slots()[0])
    params = FakeParams(session)
    await tools.book_appointment(
        params,
        name="Elif Kaya",
        phone="+49170444",
        slot_id=free["slot_id"],
        location="Harburg",
    )
    check(
        "books from an offered id",
        params.result.get("booked") is True,
        str(params.result.get("say"))[:40],
    )

    params = FakeParams(session)
    await tools.book_appointment(
        params,
        name="Gul Demir",
        phone="+49170555",
        slot_id="not-a-real-slot-id",
        location="Harburg",
    )
    check(
        "a bogus id is refused, with alternatives rather than a wrong booking",
        params.result.get("booked") is False and bool(params.result.get("alternatives")),
        params.result.get("say", "")[:56],
    )

    print("\n8bb. Preflight catches a retired model before a call does")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from preflight import BAD, OK, WARN, judge_google_model

    listing = {
        "models": [
            {"name": "models/gemini-3.6-flash", "supportedGenerationMethods": ["generateContent"]},
            {
                "name": "models/gemini-3.5-flash-lite",
                "supportedGenerationMethods": ["generateContent"],
            },
            {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
        ]
    }
    status, detail = judge_google_model("gemini-3.6-flash", listing)
    check("accepts an available model", status == OK, detail)

    status, detail = judge_google_model("gemini-2.5-flash", listing)
    check("rejects a retired model", status == BAD, detail)
    check(
        "names a usable replacement",
        "gemini-3.6-flash" in detail,
        detail,
    )
    check(
        "does not suggest an embedding model",
        "embedding" not in detail,
        detail,
    )
    status, _ = judge_google_model("anything", {"models": []})
    check("warns rather than blocks when listing fails", status == WARN)

    print("\n8c. Transcripts stay separate per call")
    t1 = store.start_call(None, "+4900001")
    t2 = store.start_call(None, "+4900002")
    store.add_transcript_line(t1, "user", "erste")
    store.add_transcript_line(t1, "assistant", "antwort")
    store.add_transcript_line(t2, "user", "zweite")
    check(
        "two calls do not share a transcript",
        len(store.transcript_for(t1)) == 2 and len(store.transcript_for(t2)) == 1,
        f"{len(store.transcript_for(t1))} / {len(store.transcript_for(t2))}",
    )

    print("\n8e. Dates in BOTH languages the bot speaks")
    # The bot switches to English mid-call and the model then passes English
    # weekday names. A German-only parser answered "I do not understand the
    # day", the model relayed that as "not available", and the caller spent two
    # minutes guessing times that were never the problem.
    for spoken, expect_parsed in [
        ("Dienstag", True),
        ("Tuesday", True),
        ("Monday", True),
        ("Friday", True),
        ("friday", True),
        ("morgen", True),
        ("tomorrow", True),
        ("2026-09-21", True),
        ("nonsense", False),
    ]:
        got = store.parse_spoken_date(spoken)
        check(f"parses {spoken!r}", (got is not None) == expect_parsed, str(got))

    check(
        "German and English name the same day",
        store.parse_spoken_date("Dienstag") == store.parse_spoken_date("Tuesday"),
    )

    print("\n8f. An unreadable day offers slots instead of dead-ending")
    params = FakeParams(session)
    await tools.check_available_appointments(params, day="whenever-ish")
    check(
        "still offers real slots",
        bool(params.result.get("slots")),
        f"{len(params.result.get('slots', []))} offered",
    )
    check(
        "never asks the caller to guess again",
        "nicht verstanden" not in params.result.get("say", ""),
        params.result.get("say", "")[:56],
    )

    print("\n8g. Slots carry an id, so the model never does time arithmetic")
    params = FakeParams(session)
    await tools.check_available_appointments(params)
    offered = params.result["slots"]
    check("every slot has an id", all("slot_id" in s_ for s_ in offered))
    check(
        "both languages precomputed",
        all("say_german" in s_ and "say_english" in s_ for s_ in offered),
    )
    check(
        "English time is correct, not model arithmetic",
        all(
            ("4 p.m." in s_["say_english"] and "16 Uhr" in s_["say_german"])
            or "16 Uhr" not in s_["say_german"]
            for s_ in offered
        ),
        offered[0]["say_english"] if offered else "",
    )
    params = FakeParams(session)
    await tools.book_appointment(
        params,
        name="Ida Roth",
        phone="+49170777",
        slot_id="2026-09-22T15:00",
        location="Barmbek",
    )
    check("refuses an id it never offered", params.result.get("booked") is False)

    print("\n8d. Speaking rules the first real call broke")
    prompt_text = system_prompt()
    for label, marker in [
        ("caps answer length in words", "hoechstens 30 Woerter"),
        ("says the cap is a ceiling, not a target", "Obergrenze, kein Ziel"),
        ("handles small talk without re-asking", "nur plaudert"),
        ("forbids the same question twice", "NIE zweimal hintereinander"),
        ("bans a repeated opening word", "demselben Wort"),
        ("never invents appointment times", "nie selbst Uhrzeiten ausdenken"),
        ("shows a too-long answer as the bad example", "dreizehn Sekunden"),
        ("forbids reading out lists", "Keine Listen vorlesen"),
        ("one question per turn", "Nur eine Frage pro Antwort"),
        ("forbids narrating its own next action", "Nicht ankuendigen"),
        ("confirm details once, not repeatedly", "genau einmal bestaetigen"),
        ("bans 'sixteen o'clock' in English", "sixteen o'clock"),
        ("gives the English time form", "four p.m."),
        ("stops asserting what the caller wants", "Stelle nicht fest, was der Anrufer will"),
    ]:
        check(label, marker in prompt_text)

    print("\n9. The bot must not state things we never confirmed")
    prompt = system_prompt()
    check(
        "does not assert theory lesson times",
        "Montag und Mittwoch" not in prompt,
        "invented schedule would be spoken as fact",
    )
    check("tells the model it does not know them", "Unterrichtszeiten kennst du NICHT" in prompt)
    check(
        "never offers office hours as lesson times",
        "nicht dieselben Zeiten" in prompt,
    )
    check(
        "only promises confirmed licence classes",
        set(config.confirmed_classes()) == {"B", "BF17"},
        ", ".join(config.confirmed_classes()),
    )
    check(
        "explains unconfirmed classes without promising them",
        "sage nie zu, dass wir sie ausbilden" in prompt,
    )
    check("still refuses to quote a price", "Preise NICHT" in prompt)

    print("\n10. Config sanity")
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
        "all branch addresses known",
        all(entry["address"] for entry in config.LOCATIONS.values()),
        "missing: " + ", ".join(n for n, e in config.LOCATIONS.items() if not e["address"]),
    )
    todo(
        "prices confirmed by owner",
        config.PRICES_CONFIRMED,
        "PRICES_CONFIRMED is False -- the bot refuses to quote any price",
    )
    todo(
        "theory lesson times confirmed",
        config.THEORY_CONFIRMED and bool(config.THEORY["schedule"]),
        "THEORY_CONFIRMED is False -- the bot refuses to state lesson times",
    )
    todo(
        "all licence classes confirmed",
        not config.unconfirmed_classes(),
        "unconfirmed: " + ", ".join(config.unconfirmed_classes()),
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
