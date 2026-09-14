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
    "legal_name": "Fahrschule Infinity GmbH",
    "city": "Hamburg",
    # Central line for all branches. Confirmed: matches the number given by the
    # owner and two public directory listings.
    "phone": "+49 40 64421700",
    "email": "info@fahrschule-infinity.de",
    "website": "https://fahrschule-infinity.de",
    # The Hamburg licensing authority. Correct for Hamburg, keep as-is.
    "authority": "Landesbetrieb Verkehr (LBV)",
}

# Infinity runs four branches that all share the one phone number above, so the
# first thing to establish on most calls is WHICH branch the caller means.
# Sources: public directory listings and the school's own social posts. Two
# addresses are still unknown -- the bot says the branch name and offers a
# callback rather than guessing a street.
LOCATIONS = {
    "Barmbek": {
        "address": "Bramfelder Strasse 95, 22305 Hamburg",
        "note": "",
    },
    "Harburg": {
        "address": "Hannoversche Strasse 86, 21079 Hamburg",
        "note": "im Untergeschoss des Phoenix-Centers",
    },
    # TODO(owner): street address missing. Until it is filled in, the bot names
    # the branch but never invents a street.
    "Billstedt": {"address": "", "note": ""},
    "Langenhorn": {"address": "", "note": ""},
}

# Branch the caller reaches if they do not say which one they mean.
DEFAULT_LOCATION = "Barmbek"


def location_address(name: str) -> str:
    """Speakable address for a branch, or an empty string if not yet known."""
    entry = LOCATIONS.get(name) or {}
    address, note = entry.get("address", ""), entry.get("note", "")
    if not address:
        return ""
    return f"{address} ({note})" if note else address


def locations_sentence() -> str:
    """All branches as one speakable sentence, addresses only where known."""
    parts = []
    for name in LOCATIONS:
        address = location_address(name)
        parts.append(f"{name}, {address}" if address else name)
    return "; ".join(parts)


# The AI's own persona. "Sie" is the safe default for a German business.
ASSISTANT_NAME = "Mia"
FORM_OF_ADDRESS = "Sie"  # "Sie" (formal) or "du" (informal)

# --- Opening hours --------------------------------------------------------
# 24h clock. None = closed. Used both for what the bot says and to decide
# whether a "call you back" promise is realistic today.

OPENING_HOURS = {
    "Montag": ("13:00", "19:00"),
    "Dienstag": ("13:00", "19:00"),
    "Mittwoch": ("13:00", "19:00"),
    "Donnerstag": ("13:00", "19:00"),
    "Freitag": ("13:00", "19:00"),
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

# Set to True ONLY once the owner has confirmed every figure below.
# While it is False the bot states no price at all and offers a callback.
#
# Why this is not simply filled in from the website: public sources disagree.
# One listing gives an Anmeldegebuehr of 395 Euro "statt 595", the school's own
# social post gives 295 Euro "statt 595" for the Langenhorn opening. Those are
# rotating promotions that differ by branch and by month. A receptionist that
# quotes last quarter's offer costs more trust than one that says "das sagt
# Ihnen ein Kollege genau".
PRICES_CONFIRMED = False

# Found publicly, NOT confirmed. Correct these, then flip the flag above.
# Written the way they should be SPOKEN, not the way they are printed.
PRICES = {
    "grundbetrag": "",  # web: 395 Euro Anmeldegebuehr, statt 595 (promotion, unconfirmed)
    "fahrstunde": "",  # web: 65 Euro pro Fahrstunde (unconfirmed)
    "sonderfahrt": "",  # unknown
    "vorstellung_praktisch": "",  # unknown
    "vorstellung_theorie": "",  # unknown
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

# The office line. "Put me through to a person" redirects the live call here.
# Careful: if the AI is answering *because* this number was busy, transferring
# back to it just bounces the caller. The bot therefore only offers a transfer
# when the caller explicitly asks for a human.
TRANSFER_NUMBER = os.getenv("TRANSFER_NUMBER", "+49 40 64421700")

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
