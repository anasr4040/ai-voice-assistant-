"""Business facts the receptionist is allowed to state on the phone.

=============================================================================
!!  EVERY VALUE IN THIS FILE IS A PLACEHOLDER INVENTED FOR THE DEMO.       !!
!!  It is typical for a Hamburg Fahrschule but it is NOT Infinity's data.  !!
!!  Replace it with the real numbers BEFORE the owner hears the demo, or   !!
!!  the bot will confidently quote prices that do not exist.               !!
!!  Anything still marked TODO is a question for the owner.                !!
=============================================================================

This is the single source of truth for the bot. The system prompt is generated
from it, so editing here changes what the receptionist says. No prompt editing
needed.
"""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

TIMEZONE = ZoneInfo("Europe/Berlin")

# --- Identity -------------------------------------------------------------

BUSINESS = {
    "name": "Fahrschule Infinity",
    "city": "Hamburg",
    # TODO(owner): real street address
    "address": "Musterstrasse 1, 20095 Hamburg",
    # TODO(owner): real callback number the office actually answers
    "phone": "+49 40 000000",
    "email": "info@fahrschule-infinity.de",
    # The Hamburg licensing authority. Correct for Hamburg, keep as-is.
    "authority": "Landesbetrieb Verkehr (LBV)",
}

# The AI's own persona. "Sie" is the safe default for a German business.
ASSISTANT_NAME = "Mia"
FORM_OF_ADDRESS = "Sie"  # "Sie" (formal) or "du" (informal)

# --- Opening hours --------------------------------------------------------
# 24h clock. None = closed. Used both for what the bot says and to decide
# whether a "call you back" promise is realistic today.

OPENING_HOURS = {
    "Montag": ("15:00", "19:00"),
    "Dienstag": ("15:00", "19:00"),
    "Mittwoch": ("15:00", "19:00"),
    "Donnerstag": ("15:00", "19:00"),
    "Freitag": ("15:00", "18:00"),
    "Samstag": None,
    "Sonntag": None,
}

WEEKDAY_NAMES = [
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
]

# --- Theory lessons -------------------------------------------------------

THEORY = {
    "schedule": "Montag und Mittwoch, jeweils 18:00 bis 19:30 Uhr",
    "lessons_required": "14 Doppelstunden Grundstoff plus 2 Doppelstunden Zusatzstoff fuer Klasse B",
    "note": "Quereinstieg jederzeit moeglich, man muss nicht auf einen neuen Kurs warten.",
}

# --- Licence classes ------------------------------------------------------
# `key` is what the LLM matches on. Keep descriptions short: they get spoken.

LICENCE_CLASSES = {
    "B": "Normaler Autofuehrerschein, Schaltwagen und Automatik.",
    "B197": "Ausbildung auf Automatik, Pruefung auf Automatik, danach darf man trotzdem Schaltwagen fahren.",
    "BF17": "Begleitetes Fahren ab 17, Fuehrerschein mit 17 mit einer Begleitperson.",
    "BE": "Klasse B mit groesserem Anhaenger.",
    "B96": "Aufbauseminar fuer schwerere Anhaenger, keine eigene Pruefung.",
    "AM": "Roller und Mofa ab 15 Jahren.",
    "A1": "Leichtkraftrad bis 125 Kubikzentimeter, ab 16.",
    "A2": "Mittlere Motorraeder, ab 18.",
    "A": "Alle Motorraeder, ab 24 oder ab 20 mit zwei Jahren A2.",
}

# --- Prices ---------------------------------------------------------------
# TODO(owner): every one of these is a guess. Replace before the demo.
# Written the way they should be SPOKEN, not the way they are printed.

PRICES = {
    "grundbetrag": "420 Euro Grundbetrag, darin ist der komplette Theorieunterricht enthalten",
    "fahrstunde": "65 Euro pro Fahrstunde zu 45 Minuten",
    "sonderfahrt": "78 Euro pro Sonderfahrt, also Ueberland, Autobahn und Nachtfahrt",
    "vorstellung_praktisch": "290 Euro Vorstellungsentgelt fuer die praktische Pruefung",
    "vorstellung_theorie": "120 Euro Vorstellungsentgelt fuer die Theoriepruefung",
    "note": (
        "Dazu kommen die amtlichen Gebuehren von TUEV oder DEKRA und vom "
        f"{BUSINESS['authority']}, die zahlt man direkt dort. "
        "Wie viele Fahrstunden jemand braucht ist sehr individuell, "
        "deshalb nennen wir am Telefon keinen Gesamtpreis."
    ),
}

# --- What a new student has to bring -------------------------------------

REQUIREMENTS = [
    "Sehtest bei einem Optiker",
    "Erste-Hilfe-Kurs mit 9 Unterrichtseinheiten",
    "ein biometrisches Passfoto",
    "Personalausweis oder Reisepass",
    f"den Antrag beim {BUSINESS['authority']}, dabei helfen wir im Buero",
]

# --- Appointment booking --------------------------------------------------

# Only free-consultation slots are bookable by phone. Driving lessons are
# deliberately NOT bookable: they depend on instructor availability.
BOOKABLE = {
    "type": "kostenloses Beratungsgespraech im Buero",
    "duration_minutes": 30,
    # Slots offered, per weekday. Must fall inside OPENING_HOURS.
    "slots": {
        "Montag": ["16:00", "17:00", "18:00"],
        "Dienstag": ["16:00", "17:00", "18:00"],
        "Mittwoch": ["16:00", "17:00", "18:00"],
        "Donnerstag": ["16:00", "17:00", "18:00"],
        "Freitag": ["16:00", "17:00"],
    },
    "book_days_ahead": 14,
}

# --- Escalation -----------------------------------------------------------

# TODO(owner): the number a caller gets transferred to when they ask for a
# human. Leave empty and the bot takes a callback request instead, which is
# the safer default for a demo.
TRANSFER_NUMBER = os.getenv("TRANSFER_NUMBER", "")

# Topics the bot must never improvise on -> take a callback request instead.
ESCALATE_TOPICS = [
    "Beschwerden oder Aerger mit einem Fahrlehrer",
    "Rueckerstattungen, Mahnungen oder Vertragsaufloesung",
    "MPU, Fuehrerscheinentzug oder Punkte in Flensburg",
    "Umschreibung eines auslaendischen Fuehrerscheins",
    "verbindliche Zusagen zu Pruefungsterminen",
]


# --- Helpers used by the prompt builder and the tools ---------------------


def now() -> datetime:
    """Current local time in Hamburg."""
    return datetime.now(TIMEZONE)


def today_name() -> str:
    """German weekday name for today."""
    return WEEKDAY_NAMES[now().weekday()]


def is_open(at: datetime | None = None) -> bool:
    """Whether the office is staffed right now."""
    at = at or now()
    hours = OPENING_HOURS.get(WEEKDAY_NAMES[at.weekday()])
    if not hours:
        return False
    start, end = hours
    return start <= at.strftime("%H:%M") < end


def speak_time(hhmm: str) -> str:
    """'15:00' -> '15 Uhr', '18:30' -> '18 Uhr 30'. TTS reads these correctly."""
    hour, minute = hhmm.split(":")
    return f"{int(hour)} Uhr" if minute == "00" else f"{int(hour)} Uhr {int(minute)}"


def hours_sentence() -> str:
    """Opening hours as one speakable German sentence, collapsing equal days."""
    groups: list[tuple[list[str], tuple[str, str]]] = []
    for day in WEEKDAY_NAMES:
        hours = OPENING_HOURS[day]
        if hours is None:
            continue
        if groups and groups[-1][1] == hours:
            groups[-1][0].append(day)
        else:
            groups.append(([day], hours))

    parts = []
    for days, (start, end) in groups:
        span = days[0] if len(days) == 1 else f"{days[0]} bis {days[-1]}"
        parts.append(f"{span} von {speak_time(start)} bis {speak_time(end)}")
    return ", ".join(parts)
