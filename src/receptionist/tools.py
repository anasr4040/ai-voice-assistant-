"""The four things the receptionist can actually DO, beyond answering questions.

These are Pipecat "direct functions": the signature and the Google-style
docstring below ARE the schema the LLM sees, so the docstrings are prompt text
-- edit them as carefully as the system prompt.

Every tool returns a `say` field. The LLM is free to reword it, but it means a
tool can steer the next sentence (for example, refusing a slot that was taken
one second ago) without that logic leaking into the system prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger
from pipecat.frames.frames import EndWorkerFrame, TTSSpeakFrame
from pipecat.services.llm_service import FunctionCallParams

from receptionist import config, notify, store, telephony


@dataclass
class CallSession:
    """Per-call state, handed to the tools via PipelineWorker(app_resources=...)."""

    call_sid: str | None = None
    caller_id: str | None = None
    transferring: bool = False
    captured: list[str] = field(default_factory=list)

    def note(self, what: str) -> None:
        """Record an outcome so the end-of-call log says what the call achieved."""
        self.captured.append(what)
        logger.info(f"[call {self.call_sid or 'web'}] {what}")


def _speak_slot(slot: dict) -> str:
    """One bookable slot, phrased for speech."""
    return f"{slot['weekday']} um {config.speak_time(slot['time'])}"


async def check_available_appointments(params: FunctionCallParams, day: str = "") -> None:
    """Look up free slots for a free consultation appointment at the driving school.

    Call this before booking, and whenever the caller asks when they could come in.
    Returns the next available slots.

    Args:
        day: Optional. The day the caller asked for, exactly as they said it,
            for example "Dienstag", "morgen" or "15.10.". Leave empty to get the
            next available slots across all days.
    """
    slots = store.free_slots()

    if day:
        wanted = store.parse_spoken_date(day)
        if wanted is None:
            await params.result_callback(
                {
                    "understood": False,
                    "say": "Ich habe den Tag nicht verstanden. Bitte noch einmal, welcher Tag?",
                }
            )
            return
        matching = [s for s in slots if s["date"] == wanted]
        if not matching:
            await params.result_callback(
                {
                    "available": False,
                    "requested_day": wanted,
                    "alternatives": [_speak_slot(s) for s in slots[:3]],
                    "say": "An dem Tag ist leider nichts frei. Biete die Alternativen an.",
                }
            )
            return
        slots = matching

    offer = slots[:3]
    await params.result_callback(
        {
            "available": bool(offer),
            "slots": [
                {"date": s["date"], "time": s["time"], "spoken": _speak_slot(s)} for s in offer
            ],
            "say": (
                "Nenne hoechstens zwei dieser Termine und frage, welcher passt."
                if offer
                else "In den naechsten zwei Wochen ist nichts frei. Biete einen Rueckruf an."
            ),
        }
    )


async def book_appointment(
    params: FunctionCallParams, name: str, phone: str, day: str, time: str, location: str
) -> None:
    """Book a free consultation appointment at one branch of the driving school.

    Only call this after the caller has confirmed a specific day, time and
    branch, and after you have read their phone number back to them. Never
    invent a slot -- use check_available_appointments first.

    Args:
        name: The caller's full name as they said it.
        phone: The caller's phone number, digits only, with country code if given.
        day: The agreed day, for example "Dienstag", "morgen" or "2026-10-15".
        time: The agreed time in 24 hour format, for example "16:00".
        location: Which branch the caller wants: Barmbek, Billstedt, Harburg or
            Langenhorn. Ask the caller if they have not said.
    """
    session: CallSession = params.app_resources
    iso_date = store.parse_spoken_date(day)

    branch = next(
        (name for name in config.LOCATIONS if name.lower() == (location or "").strip().lower()),
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

    if iso_date is None:
        await params.result_callback(
            {"booked": False, "say": "Der Tag war unklar. Frage noch einmal nach dem Tag."}
        )
        return

    normalised = time.strip().replace(".", ":")
    if len(normalised) == 2 and normalised.isdigit():
        normalised = f"{normalised}:00"

    # Only ever book a slot the school actually offers and that is still free.
    if not any(s["date"] == iso_date and s["time"] == normalised for s in store.free_slots()):
        await params.result_callback(
            {
                "booked": False,
                "alternatives": [_speak_slot(s) for s in store.free_slots()[:3]],
                "say": "Dieser Termin ist nicht mehr frei. Entschuldige dich kurz und biete eine Alternative an.",
            }
        )
        return

    booking_id = store.add_booking(
        call_sid=session.call_sid,
        name=name,
        phone=phone or session.caller_id or "",
        slot_date=iso_date,
        slot_time=normalised,
        location=branch,
        topic="Beratungsgespraech",
    )
    if booking_id is None:
        await params.result_callback(
            {"booked": False, "say": "Der Termin wurde gerade vergeben. Biete eine Alternative an."}
        )
        return

    session.note(f"Termin gebucht: {name}, {iso_date} {normalised}, Filiale {branch}")
    await notify.notify_booking(
        name=name,
        phone=phone or session.caller_id or "",
        slot_date=iso_date,
        slot_time=normalised,
        topic="Beratungsgespraech",
    )

    address = config.location_address(branch)
    where = f"in der Filiale {branch}" + (f", {address}" if address else "")
    await params.result_callback(
        {
            "booked": True,
            "date": iso_date,
            "time": normalised,
            "location": branch,
            "say": (
                f"Bestaetige den Termin in einem Satz: {name}, {iso_date} um "
                f"{config.speak_time(normalised)}, {where}. "
                + ("" if address else "Die genaue Adresse schickt ein Kollege per SMS nach. ")
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
            {"saved": False, "say": "Frage nach der Rueckrufnummer, sie fehlt noch."}
        )
        return

    store.add_lead(
        call_sid=session.call_sid,
        caller_id=session.caller_id,
        name=name,
        phone=number,
        topic=topic,
    )
    session.note(f"Rueckruf notiert: {name} ({number}) -- {topic}")
    await notify.notify_lead(name=name, phone=number, topic=topic, caller_id=session.caller_id)

    when = "heute noch" if config.is_open() else "am naechsten Werktag"
    await params.result_callback(
        {
            "saved": True,
            "say": f"Bestaetige kurz, dass sich ein Kollege {when} meldet, und frage, ob du sonst noch helfen kannst.",
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
                    "Eine Weiterleitung ist gerade nicht moeglich. Entschuldige dich kurz, "
                    "und nimm mit take_callback_request Name und Nummer auf."
                ),
            }
        )
        return

    session.transferring = True
    session.note(f"Weiterleitung an {config.TRANSFER_NUMBER}: {reason}")
    await params.result_callback({"transferred": True, "say": ""})

    # Said by us before the line is handed over -- after the redirect, Twilio owns
    # the call and our pipeline is gone.
    await params.llm.push_frame(TTSSpeakFrame("Einen Moment bitte, ich verbinde Sie."))

    transferred = await telephony.transfer_call(
        session.call_sid,
        config.TRANSFER_NUMBER,
        say_first="Sie werden jetzt verbunden.",
    )
    if transferred:
        await params.llm.push_frame(EndWorkerFrame())
    else:
        session.transferring = False
        await params.llm.push_frame(
            TTSSpeakFrame(
                "Es meldet sich leider niemand. Ich notiere Ihnen einen Rueckruf. Wie heissen Sie?"
            )
        )


TOOLS = [
    check_available_appointments,
    book_appointment,
    take_callback_request,
    transfer_to_staff,
]
