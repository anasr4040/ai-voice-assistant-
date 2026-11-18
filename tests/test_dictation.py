"""Dictation: phone numbers, email addresses, and the booking flow around them.

Run: uv run python tests/test_dictation.py

Built from a real test call that went wrong: the bot cut in on every pause,
dropped a digit of the phone number, never understood "at the rate", and the
appointment was never saved because it waited for an email that never parsed.
Every input below marked "from the test call" is what speech-to-text actually
produced in that call.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "dictation.db")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from loguru import logger  # noqa: E402

logger.remove()

from pipecat.frames.frames import TTSSpeakFrame, UserIdleTimeoutUpdateFrame  # noqa: E402

from receptionist import dictation, notify, store, tools  # noqa: E402
from receptionist.tools import CallSession  # noqa: E402

PASSED, FAILED = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{f' -- {detail}' if detail else ''}")


class FakeParams:
    """Stands in for Pipecat's FunctionCallParams and records what a tool did."""

    def __init__(self, session: CallSession):
        self.app_resources = session
        self.llm = self
        self.result: dict | None = None
        self.properties = None
        self.frames: list = []

    async def result_callback(self, result, properties=None):
        self.result, self.properties = result, properties

    async def push_frame(self, frame, direction=None):
        self.frames.append(frame)

    def spoken(self) -> str:
        return " ".join(f.text for f in self.frames if isinstance(f, TTSSpeakFrame))

    def model_runs_again(self) -> bool:
        return self.properties is None or self.properties.run_llm is not False

    def idle_timeouts(self) -> list[float]:
        return [f.timeout for f in self.frames if isinstance(f, UserIdleTimeoutUpdateFrame)]


def parser_checks() -> None:
    print("\n1. Phone numbers, from words to digits")
    for spoken, expected in [
        # From the test call, in three pieces. The model dropped the second 9.
        ("Plus four nine, one five two, one nine. Four eight six seven two.", "+491521948672"),
        ("plus vier neun eins fünf zwei eins neun vier acht sechs sieben zwei", "+491521948672"),
        ("null eins sieben zwei, vierundzwanzig, achtundneunzig", "017224 98".replace(" ", "")),
        ("0172 982 2990", "01729822990"),
        ("0049 40 6442 1700", "+494064421700"),
        ("double five", "55"),
        ("oh one seven two", "0172"),
        ("Oh, okay, plus four nine one", "+491"),
        ("my number is", ""),
    ]:
        got = dictation.parse_spoken_phone(spoken)
        check(f"{spoken!r} -> {expected!r}", got == expected, got)

    check("+49 mobile, 12 digits, is complete", dictation.phone_looks_complete("+491521948672"))
    check("half a number is not", not dictation.phone_looks_complete("+49152"))
    check("Hamburg landline is complete", dictation.phone_looks_complete("04064421700"))
    check(
        "read back digit by digit, never as one big number",
        dictation.phone_read_back("+491521948672") == "+4 9, 1 5 2, 1 9 4 8, 6 7 2",
        dictation.phone_read_back("+491521948672"),
    )

    print("\n2. Email addresses, from words to an address")
    for spoken, expected in [
        # All four from the test call.
        ("uni at the rate Gmail dot com.", "uni@gmail.com"),
        (".Rabbani.@theredgmail.com.", "rabbani@gmail.com"),
        ("it's a n a s, dot rabbani dot uni at gmail dot com.", "anas.rabbani.uni@gmail.com"),
        ("No. It's... anas dot rabbani dot uni at gmail dot com.", "anas.rabbani.uni@gmail.com"),
        ("anas punkt rabbani at gmail punkt com", "anas.rabbani@gmail.com"),
        ("max klammeraffe web punkt de", "max@web.de"),
        ("anas underscore r at gmx dot de", "anas_r@gmx.de"),
        ("ANAS at GMAIL dot COM", "anas@gmail.com"),
    ]:
        got = dictation.normalize_spoken_email(spoken)
        check(f"{spoken!r} -> {expected!r}", got == expected, str(got))
    for junk in ["", "uhh my email", "at gmail dot com", "anas rabbani at gmail com"]:
        check(
            f"refuses {junk!r} rather than guessing", dictation.normalize_spoken_email(junk) is None
        )

    check("'Dot' alone is still mid-dictation", dictation.email_looks_incomplete("Dot"))
    check(
        "no domain ending yet is mid-dictation", dictation.email_looks_incomplete("anas at gmail")
    )
    check("a full address is not", not dictation.email_looks_incomplete("anas at gmail dot com"))
    check(
        "spelled back letter by letter, symbols in the call's language",
        dictation.email_read_back("anas.r@gmail.com", "de")
        == "A N A S, Punkt, R, ät, gmail Punkt com",
        dictation.email_read_back("anas.r@gmail.com", "de"),
    )
    check(
        "and in English",
        dictation.email_read_back("anas.r@gmail.com", "en") == "A N A S, dot, R, at, gmail dot com",
    )


async def phone_tool_checks() -> None:
    print("\n3. A caller dictating a phone number in pieces")
    session = CallSession(caller_id=None)
    session.language.choose("en")

    params = FakeParams(session)
    await tools.read_back_phone_number(params, spoken="Plus four nine,")
    check("a pause mid-number gets 'Mhm.'", params.spoken() == "Mhm.", params.spoken())
    check("and nothing from the model", not params.model_runs_again())
    check("the call is now mid-dictation", session.dictating == "phone")
    check(
        "silence now means 'done', after a short wait",
        params.idle_timeouts() == [tools.DICTATION_IDLE_SECONDS],
        str(params.idle_timeouts()),
    )

    params = FakeParams(session)
    await tools.read_back_phone_number(params, spoken="Plus four nine, one five two,")
    check("still mid-number: another 'Mhm.'", params.spoken() == "Mhm.")
    check("the idle timer is not reset twice", params.idle_timeouts() == [])

    params = FakeParams(session)
    await tools.read_back_phone_number(
        params, spoken="Plus four nine, one five two, one nine. Four eight six seven two."
    )
    check(
        "the full number is read back by the tool, digit by digit",
        params.spoken() == "So that's +4 9, 1 5 2, 1 9 4 8, 6 7 2. Is that right?",
        params.spoken(),
    )
    check("the model does not reword the read-back", not params.model_runs_again())
    check("the confirmed number is remembered", session.read_back_phone == "+491521948672")
    check("dictation is over", session.dictating is None)
    check(
        "and the normal idle check-in is back",
        params.idle_timeouts() == [session.idle_seconds],
        str(params.idle_timeouts()),
    )

    session = CallSession()
    params = FakeParams(session)
    await tools.read_back_phone_number(params, spoken="zero one", finished=True)
    check("too few digits when finished: ask again", params.result.get("status") == "unclear")
    check("and let the model word that", params.model_runs_again())

    session = CallSession()
    params = FakeParams(session)
    await tools.read_back_phone_number(
        params, spoken="null vier null, sechs vier vier", finished=True
    )
    check(
        "a short number is read back when the caller says they are done",
        params.result.get("status") == "read_back" and params.spoken().startswith("Also:"),
        params.spoken(),
    )


async def booking_flow_checks() -> None:
    print("\n4. Book first, then the email -- never the other way round")
    session = CallSession(caller_id=None)
    session.read_back_phone = "+491521948672"

    params = FakeParams(session)
    slot = tools._describe_slot(store.free_slots()[0])
    await tools.book_appointment(
        params,
        name="Anas Rabbani",
        phone="+49 152 148672",  # what the model wrote in the test call: one 9 missing
        slot_id=slot["slot_id"],
        location="Barmbek",
    )
    check("booked without any email", params.result.get("booked") is True)
    booked = [b for b in store.recent_bookings() if b["name"] == "Anas Rabbani"]
    check(
        "the number the caller confirmed is stored, not the model's version",
        bool(booked) and booked[0]["phone"] == "+491521948672",
        booked[0]["phone"] if booked else "no row",
    )
    check("the call counts as captured straight away", bool(session.captured))

    saved = notify.RESEND_API_KEY, notify.RESEND_FROM
    notify.RESEND_API_KEY, notify.RESEND_FROM = "", "onboarding@resend.dev"
    check(
        "with no way to email callers, Mia does not ask for an email",
        "NICHT nach einer E-Mail" in params.result.get("say", ""),
    )
    notify.RESEND_API_KEY, notify.RESEND_FROM = "re_test", "mia@fahrschule-infinity.de"
    params = FakeParams(session)
    slot = tools._describe_slot(store.free_slots()[0])
    await tools.book_appointment(
        params, name="Lena Berg", phone="+4917011", slot_id=slot["slot_id"], location="Barmbek"
    )
    check(
        "with email set up, she offers it after booking",
        "per E-Mail möchte" in params.result.get("say", ""),
    )

    print("\n5. The email, at most twice")
    sent: list[str] = []

    async def fake_confirm(*, to_email, name, when, where):
        sent.append(to_email)
        return True

    saved_confirm = notify.confirm_booking_to_caller
    notify.confirm_booking_to_caller = fake_confirm
    session.language.choose("en")

    params = FakeParams(session)
    await tools.read_back_email(params, spoken="Dot")
    check("a fragment gets 'Mhm.'", params.spoken() == "Mhm." and session.dictating == "email")

    params = FakeParams(session)
    await tools.read_back_email(params, spoken="Dot Rabani dot uni at the rate Gmail dot com.")
    check(
        "'at the rate' is understood and spelled back",
        params.spoken() == "So that's R A B A N I, dot, U N I, at, gmail dot com. Is that right?",
        params.spoken(),
    )

    params = FakeParams(session)
    await tools.read_back_email(params, spoken="anas dot rabbani dot uni at gmail dot com")
    check("second try is read back", session.read_back_email == "anas.rabbani.uni@gmail.com")

    params = FakeParams(session)
    await tools.read_back_email(params, spoken="something else at gmail dot com")
    check(
        "a third try is not attempted: she lets it go",
        params.result.get("status") == "give_up" and session.read_back_email is None,
        str(params.result.get("status")),
    )
    check("and does not ask again", "NICHT noch einmal" in params.result.get("say", ""))

    params = FakeParams(session)
    await tools.send_confirmation_email(params)
    check("nothing is sent to an address the caller rejected", params.result.get("sent") is False)

    fresh = CallSession()
    fresh.booking = session.booking
    fresh.language.choose("de")
    params = FakeParams(fresh)
    await tools.read_back_email(params, spoken="lena punkt berg at web punkt de")
    params = FakeParams(fresh)
    await tools.send_confirmation_email(params)
    await asyncio.sleep(0.05)  # the email goes out in the background
    check("a confirmed address is sent to", sent == ["lena.berg@web.de"], str(sent))
    stored = [b for b in store.recent_bookings() if b["id"] == fresh.booking["id"]]
    check(
        "and attached to the booking",
        bool(stored) and stored[0]["email"] == "lena.berg@web.de",
        str(stored[0]["email"]) if stored else "no row",
    )

    notify.confirm_booking_to_caller = saved_confirm
    notify.RESEND_API_KEY, notify.RESEND_FROM = saved


def turn_taking_checks() -> None:
    print("\n6. While dictating, no word is thrown away")
    session = CallSession()
    strategy = dictation.DictationAwareMinWords(session=session, min_words=2)
    check("normally two words to interrupt", strategy._min_words == 2)
    session.dictating = "email"
    check("while dictating, one word is enough", strategy._min_words == 1)
    session.dictating = None
    check("and back to two afterwards", strategy._min_words == 2)


async def main() -> int:
    store.init()
    parser_checks()
    await phone_tool_checks()
    await booking_flow_checks()
    turn_taking_checks()

    print(f"\n{'=' * 58}\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("FAILED: " + ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
