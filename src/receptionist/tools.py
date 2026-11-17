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
import re
from collections.abc import Awaitable
from dataclasses import dataclass, field

from loguru import logger
from pipecat.frames.frames import EndWorkerFrame, FunctionCallResultProperties, TTSSpeakFrame
from pipecat.services.llm_service import FunctionCallParams

from receptionist import config, notify, store, telephony
from receptionist.language import SUPPORTED, LanguagePolicy, phrase, resolve, switch_frames

# Seconds to let "one moment, I'm putting you through" play before Twilio takes
# the call away from us. Redirecting at once cut the sentence off mid-word.
TRANSFER_SPEAK_SECONDS = 3.0

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


# Spoken email addresses arrive mangled. Speech-to-text writes "at" and "punkt"
# as words, inserts spaces between every part, and never produces "@". These are
# the spoken spellings that actually turn up, German and English.
_EMAIL_AT = (" at ", " ät ", " aet ", " klammeraffe ", " atzeichen ")
_EMAIL_DOT = (" dot ", " punkt ", " period ", " point ")


def normalize_spoken_email(raw: str) -> str | None:
    """Turn a dictated address into a real one, or None if it is not usable.

    "anas punkt rabbani at gmail punkt com" -> "anas.rabbani@gmail.com"

    Returning None matters as much as parsing: a confirmation sent to a
    misheard address is worse than none, because the caller believes it is on
    its way.
    """
    text = f" {(raw or '').strip().lower()} "
    for word in _EMAIL_AT:
        text = text.replace(word, "@")
    for word in _EMAIL_DOT:
        text = text.replace(word, ".")

    # Speech-to-text spaces out the letters it spells; the address has none.
    candidate = re.sub(r"\s+", "", text).strip(".,;:!?")
    # Deepgram sometimes writes "gmail.com" as "gmail. com" -> already handled,
    # and occasionally doubles a separator when the caller pauses.
    candidate = re.sub(r"\.{2,}", ".", candidate).replace("@@", "@")

    if not re.fullmatch(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", candidate):
        return None
    return candidate


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


async def book_appointment(
    params: FunctionCallParams, name: str, phone: str, email: str, slot_id: str, location: str
) -> None:
    """Book a free consultation appointment at one branch of the driving school.

    Call check_available_appointments first and pass back one of the slot_id
    values it returned, exactly as written. Never build a slot_id yourself and
    never convert a time: an invented slot is refused, which leaves the caller
    guessing.

    Args:
        name: The caller's full name as they said it.
        phone: The caller's phone number, digits only, with country code if given.
        email: The caller's email address, for the written confirmation. Pass an
            empty string if they do not want to give one -- never invent it.
        slot_id: A slot_id from check_available_appointments, copied exactly.
        location: Which branch the caller wants: Barmbek, Billstedt, Harburg or
            Langenhorn. Ask the caller if they have not said.
    """
    session: CallSession = params.app_resources

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

    # A misheard address is worse than none: the caller then waits for a
    # confirmation that was never deliverable.
    clean_email = normalize_spoken_email(email) if email else None

    booking_id = store.add_booking(
        call_sid=session.call_sid,
        name=name,
        phone=phone or session.caller_id or "",
        slot_date=match["date"],
        slot_time=match["time"],
        location=branch,
        email=clean_email,
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

    _in_background(
        notify.notify_booking(
            name=name,
            phone=phone or session.caller_id or "",
            slot_date=match["date"],
            slot_time=match["time"],
            topic=f"Beratungsgespräch, Filiale {branch}"
            + ("" if address else " -- Adresse der Filiale bitte noch durchgeben"),
        )
    )

    confirmation_sent = bool(clean_email) and notify.can_email_callers()
    if confirmation_sent:
        _in_background(
            notify.confirm_booking_to_caller(
                to_email=clean_email, name=name, when=described["say_german"], where=where
            )
        )

    if email and not clean_email:
        # We heard something but could not make an address of it. Say so rather
        # than let the caller expect an email that will never arrive.
        email_line = (
            "Die E-Mail-Adresse hast du nicht sicher verstanden. Sage, dass die "
            "Bestätigung nur mündlich erfolgt, und biete an, sie noch einmal "
            "zu buchstabieren."
        )
    elif confirmation_sent:
        email_line = f"Sage, dass die Bestätigung gleich per E-Mail an {clean_email} kommt."
    elif clean_email:
        email_line = "Erwähne die E-Mail nicht weiter."
    else:
        email_line = ""
    await params.result_callback(
        {
            "booked": True,
            "location": branch,
            "email_confirmation_sent": confirmation_sent,
            "say": (
                f"Bestätige jetzt EINMAL, in einem Satz: {name}, "
                f"{described['say_german']} (englisch: {described['say_english']}), {where}. "
                + ("" if address else "Sage, dass ein Kollege die genaue Adresse noch durchgibt. ")
                + (f"{email_line} " if email_line else "")
                + "Frage dann, ob du sonst noch helfen kannst."
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
    number = phone or session.caller_id or ""

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
                "available": list(SUPPORTED.values()),
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


TOOLS = [
    check_available_appointments,
    book_appointment,
    take_callback_request,
    transfer_to_staff,
    switch_language,
]
