"""The things the receptionist can actually DO, beyond answering questions.

These are Pipecat "direct functions": the signature and the Google-style
docstring below ARE the schema the LLM sees, so the docstrings are prompt text
-- edit them as carefully as the system prompt.

Every tool returns a `say` field. The LLM is free to reword it, but it means a
tool can steer the next sentence (for example, refusing a slot that was taken
one second ago) without that logic leaking into the system prompt.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable
from dataclasses import dataclass, field

from loguru import logger
from pipecat.frames.frames import (
    EndWorkerFrame,
    FunctionCallResultProperties,
    TTSSpeakFrame,
    UserIdleTimeoutUpdateFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import FunctionCallParams

from receptionist import config, notify, store, telephony
from receptionist.dictation import (
    email_looks_incomplete,
    email_read_back,
    email_was_heard,
    normalize_spoken_email,
    parse_spoken_phone,
    phone_looks_complete,
    phone_read_back,
    phone_was_heard,
)
from receptionist.language import (
    SUPPORTED,
    LanguagePolicy,
    available,
    phrase,
    resolve,
    switch_frames,
)

# Seconds to let "one moment, I'm putting you through" play before Twilio takes
# the call away from us. Redirecting at once cut the sentence off mid-word.
TRANSFER_SPEAK_SECONDS = 3.0

# After a "mhm" mid-dictation, this much silence means the caller is done and
# is waiting for the read-back. Much shorter than the normal idle check-in.
DICTATION_IDLE_SECONDS = 1.5

# Times an email address is read back before giving up on it. The appointment
# is already booked by then, so giving up costs nothing but the email.
MAX_EMAIL_READ_BACKS = 2

# Strong references to fire-and-forget tasks; asyncio only keeps weak ones.
_background: set[asyncio.Task] = set()


def _in_background(job: Awaitable) -> None:
    """Run a notification without making the caller wait for it.

    Emails used to be sent before the tool returned, so the caller sat in
    silence for a second or two after every booking -- up to thirty when the
    mail server was slow. The row is already in SQLite before this runs, so
    nothing is lost if delivery fails; notify logs the failure itself.
    """
    task = asyncio.ensure_future(job)
    _background.add(task)
    task.add_done_callback(_background.discard)


@dataclass
class CallSession:
    """Per-call state, handed to the tools via PipelineWorker(app_resources=...)."""

    call_sid: str | None = None
    caller_id: str | None = None
    # Row id from store.start_call. Identifies THIS call even when there is no
    # call_sid, as in the browser demo.
    call_id: int | None = None
    transferring: bool = False
    # Bookings, callbacks, transfers: what makes the call a success.
    captured: list[str] = field(default_factory=list)
    # Anything else worth reading later -- errors, timeouts. Not a success.
    events: list[str] = field(default_factory=list)
    language: LanguagePolicy = field(default_factory=LanguagePolicy)
    # The normal idle check-in delay, restored after dictation. Set by bot.py.
    idle_seconds: float = 10.0
    # "phone" or "email" while the caller is dictating one, else None.
    dictating: str | None = None
    # What the caller heard read back. Bookings and callbacks use this rather
    # than whatever the model passes, so a confirmed number cannot be
    # re-garbled on the way into the database.
    read_back_phone: str | None = None
    read_back_email: str | None = None
    email_read_backs: int = 0
    # The appointment booked on this call, for the email confirmation after it.
    booking: dict | None = None
    # What the caller has said of the number/address being dictated, so a
    # pause can be answered with the read-back directly, without the model.
    dictation_spoken: str = ""
    # The caller's recent utterances, kept by dictation.HeardRecorder. None
    # when nothing records them; the "was it really said" checks then pass.
    heard: list[str] | None = None

    def note(self, what: str) -> None:
        """Record a capture: the call achieved something the office can act on."""
        self.captured.append(what)
        logger.info(f"[call {self.call_sid or 'web'}] {what}")

    def event(self, what: str) -> None:
        """Record something that happened, without counting it as a success."""
        self.events.append(what)
        logger.info(f"[call {self.call_sid or 'web'}] {what}")

    @property
    def outcome(self) -> str:
        """Everything that happened, captures first, for the calls table."""
        return "; ".join(self.captured + self.events)


# Weekday names for the English side of the call, so the model never translates.
_WEEKDAY_EN = {
    "Montag": "Monday",
    "Dienstag": "Tuesday",
    "Mittwoch": "Wednesday",
    "Donnerstag": "Thursday",
    "Freitag": "Friday",
    "Samstag": "Saturday",
    "Sonntag": "Sunday",
}


def _slot_id(slot: dict) -> str:
    """Opaque identifier for a slot: the only thing booking accepts.

    The model used to be handed "Dienstag um 16 Uhr" and had to convert that to
    a 12-hour English time for the caller and back to 24-hour for the booking
    call. It got 16 Uhr wrong as "3 p.m.", offered that to the caller, and then
    its own booking was refused as an unoffered slot -- two minutes of the
    caller guessing times. Passing an id back removes the arithmetic entirely.
    """
    return f"{slot['date']}T{slot['time']}"


def _describe_slot(slot: dict) -> dict:
    """A slot the model can read out in either language without converting it."""
    hour = int(slot["time"].split(":")[0])
    minute = slot["time"].split(":")[1]
    suffix = "a.m." if hour < 12 else "p.m."
    hour_12 = hour % 12 or 12
    english_time = f"{hour_12} {suffix}" if minute == "00" else f"{hour_12}:{minute} {suffix}"
    return {
        "slot_id": _slot_id(slot),
        "say_german": f"{slot['weekday']} um {config.speak_time(slot['time'])}",
        "say_english": f"{_WEEKDAY_EN.get(slot['weekday'], slot['weekday'])} at {english_time}",
        # For every other language: the model phrases these itself. 24-hour
        # time is the norm in all of them, so there is no arithmetic to get wrong.
        "date": slot["date"],
        "time_24h": slot["time"],
    }


async def check_available_appointments(params: FunctionCallParams, day: str = "") -> None:
    """Look up free slots for a free consultation appointment at the driving school.

    Call this before booking, and whenever the caller asks when they could come in.
    Returns the next available slots.

    Args:
        day: Optional. The day the caller asked for, in German or English, for
            example "Dienstag", "Tuesday", "morgen" or "15.10.". If the caller
            spoke another language, translate the day into German first. Leave
            empty to get the next available slots across all days.
    """
    await _say_one_moment(params)
    slots = store.free_slots()

    if day:
        wanted = store.parse_spoken_date(day)
        if wanted is None:
            # Offer what exists instead of dead-ending. Refusing here used to
            # send the conversation into a loop: the caller kept naming days,
            # the parser kept rejecting them, and nobody could get out.
            await params.result_callback(
                {
                    "available": bool(slots),
                    "slots": [_describe_slot(s) for s in slots[:3]],
                    "say": (
                        "Den Tag hast du nicht sicher verstanden. Nenne einfach die "
                        "nächsten freien Termine, wortwörtlich, und frage, welcher passt."
                    ),
                }
            )
            return
        matching = [s for s in slots if s["date"] == wanted]
        if not matching:
            await params.result_callback(
                {
                    "available": False,
                    "requested_day": wanted,
                    "alternatives": [_describe_slot(s) for s in slots[:3]],
                    "say": "An dem Tag ist leider nichts frei. Biete die Alternativen an.",
                }
            )
            return
        slots = matching

    offer = slots[:3]
    await params.result_callback(
        {
            "available": bool(offer),
            "slots": [_describe_slot(s) for s in offer],
            "say": (
                "Nenne höchstens zwei dieser Termine: auf Deutsch wortwörtlich "
                "say_german, auf Englisch say_english, in jeder anderen Sprache "
                "Wochentag und time_24h. Rechne Uhrzeiten NIE selbst um. Zum Buchen "
                "gibst du die slot_id genau so zurück, wie sie hier steht."
                if offer
                else "In den nächsten zwei Wochen ist nichts frei. Biete einen Rückruf an."
            ),
        }
    )


async def _say_one_moment(params: FunctionCallParams) -> None:
    """A short "one moment" while a tool runs and the model writes the answer.

    A tool means a second round-trip to the model before anything is said --
    about three quarters of a second of silence. A receptionist looking
    something up says "Moment." in that gap, so Mia does too.
    """
    session: CallSession = params.app_resources
    options = phrase("one_moment", session.language.current).split("|")
    await params.llm.push_frame(TTSSpeakFrame(random.choice(options)))


def _phone_for(session: CallSession, phone: str) -> str:
    """The number to store: the one the caller confirmed, if there was one."""
    return session.read_back_phone or parse_spoken_phone(phone) or phone or session.caller_id or ""


async def book_appointment(
    params: FunctionCallParams, name: str, phone: str, slot_id: str, location: str
) -> None:
    """Book a free consultation appointment at one branch of the driving school.

    Book as soon as you have the slot, the branch, the name and the phone
    number. Do NOT wait for an email address: the email is asked for after
    booking, so a caller who hangs up during it still has their appointment.

    Call check_available_appointments first and pass back one of the slot_id
    values it returned, exactly as written. Never build a slot_id yourself and
    never convert a time: an invented slot is refused, which leaves the caller
    guessing.

    Args:
        name: The caller's full name as they said it.
        phone: The caller's phone number. If you read it back with
            read_back_phone_number, the confirmed number is used automatically.
        slot_id: A slot_id from check_available_appointments, copied exactly.
        location: Which branch the caller wants: Barmbek, Billstedt, Harburg or
            Langenhorn. Ask the caller if they have not said.
    """
    session: CallSession = params.app_resources
    await _say_one_moment(params)

    branch = next(
        (name_ for name_ in config.LOCATIONS if name_.lower() == (location or "").strip().lower()),
        None,
    )
    if branch is None:
        await params.result_callback(
            {
                "booked": False,
                "locations": list(config.LOCATIONS),
                "say": "Frage, in welcher Filiale der Termin sein soll, und nenne die vier Standorte.",
            }
        )
        return

    # Only ever book a slot we actually offered and that is still free. Matching
    # on the id means a mistyped or invented time cannot become a wrong booking.
    match = next((s for s in store.free_slots() if _slot_id(s) == (slot_id or "").strip()), None)
    if match is None:
        alternatives = [_describe_slot(s) for s in store.free_slots()[:3]]
        await params.result_callback(
            {
                "booked": False,
                "alternatives": alternatives,
                "say": (
                    "Diese slot_id gibt es nicht oder sie ist vergeben. Nenne eine der "
                    "Alternativen und buche dann mit deren slot_id. Erfinde keine Uhrzeit."
                ),
            }
        )
        return

    number = _phone_for(session, phone)
    booking_id = store.add_booking(
        call_sid=session.call_sid,
        name=name,
        phone=number,
        slot_date=match["date"],
        slot_time=match["time"],
        location=branch,
        email=None,
        topic="Beratungsgespräch",
    )
    if booking_id is None:
        await params.result_callback(
            {"booked": False, "say": "Der Termin wurde gerade vergeben. Biete eine Alternative an."}
        )
        return

    session.note(f"Termin gebucht: {name}, {match['date']} {match['time']}, Filiale {branch}")

    address = config.location_address(branch)
    described = _describe_slot(match)
    where = f"Filiale {branch}" + (f", {address}" if address else "")
    session.booking = {
        "id": booking_id,
        "name": name,
        "when": described["say_german"],
        "where": where,
    }

    _in_background(
        notify.notify_booking(
            name=name,
            phone=number,
            slot_date=match["date"],
            slot_time=match["time"],
            topic=f"Beratungsgespräch, Filiale {branch}"
            + ("" if address else " -- Adresse der Filiale bitte noch durchgeben"),
        )
    )

    # Only offer an email confirmation that can actually be sent. With Resend's
    # shared test sender it cannot, and asking would waste the caller's time.
    next_step = (
        "Frage dann locker, ob der Anrufer die Bestätigung auch per E-Mail möchte."
        if notify.can_email_callers()
        else "Frage dann, ob du sonst noch helfen kannst. Frage NICHT nach einer E-Mail."
    )
    await params.result_callback(
        {
            "booked": True,
            "location": branch,
            "say": (
                f"Der Termin ist gespeichert. Bestätige ihn EINMAL, kurz und warm: "
                f"{described['say_german']} (englisch: {described['say_english']}), {where}. "
                + ("" if address else "Sage, dass ein Kollege die genaue Adresse noch durchgibt. ")
                + next_step
            ),
        }
    )


async def take_callback_request(
    params: FunctionCallParams, name: str, phone: str, topic: str
) -> None:
    """Record a callback request so a member of staff can phone the caller back.

    Use this whenever you cannot answer something, whenever the caller wants
    something you may not decide, and whenever the office is closed.

    Args:
        name: The caller's full name as they said it.
        phone: The callback number, digits only, with country code if given.
        topic: One short sentence on what the caller wants, in German.
    """
    session: CallSession = params.app_resources
    number = _phone_for(session, phone)
    await _say_one_moment(params)

    if not number:
        await params.result_callback(
            {"saved": False, "say": "Frage nach der Rückrufnummer, sie fehlt noch."}
        )
        return

    store.add_lead(
        call_sid=session.call_sid,
        caller_id=session.caller_id,
        name=name,
        phone=number,
        topic=topic,
    )
    session.note(f"Rückruf notiert: {name} ({number}) -- {topic}")
    _in_background(
        notify.notify_lead(name=name, phone=number, topic=topic, caller_id=session.caller_id)
    )

    when = "heute noch" if config.is_open() else "am nächsten Werktag"
    await params.result_callback(
        {
            "saved": True,
            "say": f"Bestätige kurz, dass sich ein Kollege {when} meldet, und frage, ob du sonst noch helfen kannst.",
        }
    )


async def transfer_to_staff(params: FunctionCallParams, reason: str) -> None:
    """Hand the call over to a human member of staff.

    Only call this when the caller explicitly asks for a person, or when they
    are upset. If no one is reachable this falls back to a callback request, so
    it is always safe to call.

    Args:
        reason: One short sentence, in German, on why the caller wants a human.
    """
    session: CallSession = params.app_resources

    if not (config.TRANSFER_NUMBER and session.call_sid):
        # No transfer target, or a browser demo with no real call to redirect.
        await params.result_callback(
            {
                "transferred": False,
                "say": (
                    "Eine Weiterleitung ist gerade nicht möglich. Entschuldige dich kurz, "
                    "und nimm mit take_callback_request Name und Nummer auf."
                ),
            }
        )
        return

    session.transferring = True
    language = session.language.current
    # run_llm=False: the next words are ours, below. Letting the model answer
    # the result as well made it talk over "one moment, putting you through".
    await params.result_callback(
        {"transferred": True, "say": ""},
        properties=FunctionCallResultProperties(run_llm=False),
    )

    # Said by us before the line is handed over -- after the redirect, Twilio
    # owns the call and our pipeline is gone, so give the sentence time to play.
    await params.llm.push_frame(TTSSpeakFrame(phrase("connecting", language)))
    await asyncio.sleep(TRANSFER_SPEAK_SECONDS)

    transferred = await telephony.transfer_call(session.call_sid, config.TRANSFER_NUMBER)
    if transferred:
        session.note(f"Weiterleitung an {config.TRANSFER_NUMBER}: {reason}")
        await params.llm.push_frame(EndWorkerFrame())
    else:
        session.transferring = False
        session.event("Weiterleitung fehlgeschlagen")
        await params.llm.push_frame(TTSSpeakFrame(phrase("transfer_failed", language)))


async def switch_language(params: FunctionCallParams, language: str) -> None:
    """Switch the conversation, and the voice, to another language.

    Call this ONLY when the caller explicitly asks to speak another language,
    for example "Can we speak English?", "Sprechen Sie Türkisch?" or "Türkçe
    konuşabilir miyiz?". Never call it because of a single foreign word, and
    never because you guess the caller might prefer it -- the system already
    follows the language the caller speaks.

    Args:
        language: The language the caller asked for, as a two-letter code, for
            example "en", "tr", "ar", "ru", "pl" or "de".
    """
    session: CallSession = params.app_resources
    code = resolve(language)

    if code is None:
        await params.result_callback(
            {
                "switched": False,
                "available": list(available().values()),
                "say": (
                    "Diese Sprache sprichst du leider nicht. Sage das freundlich, in der "
                    "aktuellen Sprache, und biete Deutsch oder Englisch an."
                ),
            }
        )
        return

    if session.language.choose(code):
        # The model's own prompt must change before it answers this result, so
        # the update goes into its input queue ahead of the result. The voice
        # update travels downstream like any other frame.
        llm_update, tts_update = switch_frames(code)
        await params.llm.queue_frame(llm_update)
        await params.llm.push_frame(tts_update)

    name = SUPPORTED[code]
    await params.result_callback(
        {
            "switched": True,
            "language": name,
            "say": (
                f"Sprich ab jetzt {name}. Kündige den Wechsel nicht lange an: "
                "beantworte direkt, was der Anrufer zuletzt wollte, auf "
                f"{name}, oder frage auf {name}, wie du helfen kannst."
            ),
        }
    )


async def _set_dictating(params: FunctionCallParams, kind: str | None) -> None:
    """Enter or leave dictation, and move the idle timer with it.

    While dictating, a short silence after "mhm" means the caller is done and
    waiting -- so the idle check-in fires after DICTATION_IDLE_SECONDS and
    asks for the read-back, instead of "are you still there?" after ten.
    """
    session: CallSession = params.app_resources
    if session.dictating == kind:
        return
    session.dictating = kind
    timeout = DICTATION_IDLE_SECONDS if kind else session.idle_seconds
    await params.llm.push_frame(
        UserIdleTimeoutUpdateFrame(timeout=timeout), FrameDirection.UPSTREAM
    )


async def _speak(params: FunctionCallParams, text: str, result: dict) -> None:
    """Say a fixed line ourselves and do not run the model again.

    Faster than a second model round-trip, and the words cannot be changed on
    the way -- which is the whole point for a read-back.
    """
    await params.llm.push_frame(TTSSpeakFrame(text))
    await params.result_callback(result, properties=FunctionCallResultProperties(run_llm=False))


@dataclass
class _Step:
    """What one piece of dictation leads to."""

    result: dict
    speak: str | None = None  # a fixed line to say; then the model stays quiet
    dictating: str | None = None  # still mid-dictation of this kind afterwards


def _phone_step(session: CallSession, spoken: str, finished: bool) -> _Step:
    language = session.language.current
    number = parse_spoken_phone(spoken)

    if not finished and not phone_looks_complete(number):
        return _Step({"status": "listening", "heard_so_far": number}, "Mhm.", "phone")
    if len(number.lstrip("+")) < 6:
        return _Step(
            {
                "status": "unclear",
                "say": (
                    "Die Nummer ist nicht ganz angekommen. Sage das kurz und freundlich und "
                    "bitte darum, sie langsam noch einmal zu sagen."
                ),
            }
        )
    if not phone_was_heard(number, session.heard):
        return _Step(
            {
                "status": "not_heard",
                "say": (
                    "Diese Nummer hat der Anrufer so nicht gesagt. Erfinde nichts: bitte ihn "
                    "freundlich, seine Nummer zu sagen, und warte darauf."
                ),
            }
        )
    session.read_back_phone = number
    return _Step(
        {
            "status": "read_back",
            "number": number,
            "next": (
                "Warte auf ja oder nein. Bei nein: frage nach der richtigen Nummer und rufe "
                "dieses Werkzeug mit den neuen Worten erneut auf."
            ),
        },
        f"{phrase('read_back_intro', language)} {phone_read_back(number)}. "
        f"{phrase('is_that_right', language)}",
    )


def _email_step(session: CallSession, spoken: str, finished: bool) -> _Step:
    language = session.language.current
    if not finished and email_looks_incomplete(spoken):
        return _Step({"status": "listening"}, "Mhm.", "email")

    email = normalize_spoken_email(spoken)
    if email and not email_was_heard(email, session.heard):
        # Not counted as an attempt: the caller has not been asked yet.
        return _Step(
            {
                "status": "not_heard",
                "say": (
                    "Diese Adresse hat der Anrufer nicht gesagt. Erfinde NIE eine Adresse und "
                    "nenne keine Beispiele. Bitte ihn, seine Adresse zu sagen, und warte darauf."
                ),
            }
        )
    if session.email_read_backs >= MAX_EMAIL_READ_BACKS or (
        email is None and session.email_read_backs >= MAX_EMAIL_READ_BACKS - 1
    ):
        session.read_back_email = None
        return _Step(
            {
                "status": "give_up",
                "say": (
                    "Lass die E-Mail jetzt sein. Sage locker, dass das gar kein Problem ist, "
                    "der Termin ist ja fest gebucht. Frage NICHT noch einmal nach der "
                    "E-Mail, sondern ob du sonst noch helfen kannst."
                ),
            }
        )
    session.email_read_backs += 1
    if email is None:
        return _Step(
            {
                "status": "unclear",
                "say": (
                    "Die Adresse ist nicht sicher angekommen. Bitte freundlich darum, den Teil "
                    "vor dem @ langsam Buchstabe für Buchstabe zu sagen."
                ),
            }
        )
    session.read_back_email = email
    return _Step(
        {
            "status": "read_back",
            "email": email,
            "next": (
                "Bei ja: rufe send_confirmation_email auf. Bei nein: bitte darum, die "
                "Adresse noch einmal zu sagen, und rufe dieses Werkzeug erneut auf."
            ),
        },
        f"{phrase('read_back_intro', language)} {email_read_back(email, language)}. "
        f"{phrase('is_that_right', language)}",
    )


async def _apply(params: FunctionCallParams, step: _Step, spoken: str) -> None:
    """Carry out a dictation step from inside a tool call."""
    session: CallSession = params.app_resources
    session.dictation_spoken = spoken if step.dictating else ""
    await _set_dictating(params, step.dictating)
    if step.speak:
        await _speak(params, step.speak, step.result)
    else:
        await params.result_callback(step.result)


def finish_dictation(session: CallSession) -> str | None:
    """The caller paused after a "mhm": read back what they said, without the model.

    Called by bot.py when the short dictation idle timer fires. Returns the
    line to speak, or None when the model has to word the reply (the number
    was too short, or the email is being given up on). Asking the model to
    call the tool cost a full extra round-trip, about a second of silence.
    """
    kind, spoken = session.dictating, session.dictation_spoken
    if not kind or not spoken:
        return None
    step = (_phone_step if kind == "phone" else _email_step)(session, spoken, True)
    session.dictating = None
    session.dictation_spoken = ""
    return step.speak


async def read_back_phone_number(
    params: FunctionCallParams, spoken: str, finished: bool = False
) -> None:
    """Take down a phone number the caller is dictating, and read it back.

    Call this EVERY time the caller says any part of a phone number. Pass
    EVERYTHING they have said of the number so far, word for word, with all
    the pieces joined in order (first "plus four nine one five two", then
    "plus four nine one five two one nine four eight", and so on). Do not
    convert, tidy or shorten it, and never call it with a number the caller
    has not said. This tool speaks by itself: a short "mhm" while the number
    is still incomplete, or the number read back digit by digit once it is
    complete. Say nothing yourself after calling it.

    Args:
        spoken: Everything the caller has said of the number so far, verbatim.
        finished: True when the caller says they are done, or the system tells
            you they have stopped talking. Then it is read back even if short.
    """
    session: CallSession = params.app_resources
    await _apply(params, _phone_step(session, spoken, finished), spoken)


async def read_back_email(params: FunctionCallParams, spoken: str, finished: bool = False) -> None:
    """Take down an email address the caller is dictating, and read it back.

    Only use this after the appointment is booked, when the caller wants an
    email confirmation AND has started saying their address. Never call it
    with an address the caller has not said, and never with an example. Call
    it EVERY time the caller says any part of the address, passing EVERYTHING
    they have said of it so far, word for word, all pieces joined in order.
    This tool speaks by itself: "mhm" while the address is incomplete, or the
    address spelled back once it is complete. Say nothing yourself after
    calling it.

    Args:
        spoken: Everything the caller has said of the address so far, verbatim.
        finished: True when the caller says they are done, or the system tells
            you they have stopped talking.
    """
    session: CallSession = params.app_resources
    await _apply(params, _email_step(session, spoken, finished), spoken)


async def send_confirmation_email(params: FunctionCallParams) -> None:
    """Email the caller their booking confirmation.

    Call this only after read_back_email read the address back and the caller
    said it is right. The address that was read back is used.
    """
    session: CallSession = params.app_resources
    email, booking = session.read_back_email, session.booking

    if not booking:
        await params.result_callback(
            {"sent": False, "say": "Es gibt noch keinen gebuchten Termin. Buche zuerst."}
        )
        return
    if not email:
        await params.result_callback(
            {"sent": False, "say": "Lies die Adresse zuerst mit read_back_email zurück."}
        )
        return

    await _say_one_moment(params)
    store.set_booking_email(booking["id"], email)
    session.note(f"E-Mail für Bestätigung: {email}")

    if not notify.can_email_callers():
        # A test call promised "the email is on its way" with no way to send
        # it. Keep the address for the office and say what is true.
        _in_background(
            notify.notify(
                f"Bestätigung per E-Mail gewünscht: {booking['name']}",
                f"{booking['name']} möchte die Terminbestätigung per E-Mail an {email}.\n"
                f"Termin: {booking['when']}, {booking['where']}\n"
                "Der automatische E-Mail-Versand ist noch nicht eingerichtet.",
            )
        )
        await params.result_callback(
            {
                "sent": False,
                "say": (
                    "Sage ehrlich und locker: eine automatische E-Mail-Bestätigung geht gerade "
                    "noch nicht, aber die Adresse ist notiert und der Termin fest gebucht. "
                    "Frage dann, ob du sonst noch helfen kannst."
                ),
            }
        )
        return

    _in_background(
        notify.confirm_booking_to_caller(
            to_email=email, name=booking["name"], when=booking["when"], where=booking["where"]
        )
    )
    await params.result_callback(
        {
            "sent": True,
            "say": "Sage kurz, dass die Bestätigung gleich kommt, und frage, ob du sonst noch helfen kannst.",
        }
    )


TOOLS = [
    check_available_appointments,
    book_appointment,
    take_callback_request,
    transfer_to_staff,
    switch_language,
    read_back_phone_number,
    read_back_email,
    send_confirmation_email,
]
