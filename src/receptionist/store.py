"""Persistence for everything the receptionist captures on a call.

SQLite on purpose: the demo has to survive a restart and be readable by the
office without anyone installing a database. Swap this module for the real
Fahrschul-Software API later; nothing else has to change.
"""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from receptionist import config

DB_PATH = Path(os.getenv("DB_PATH", "data/receptionist.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    call_sid    TEXT,
    caller_id   TEXT,
    name        TEXT NOT NULL,
    phone       TEXT NOT NULL,
    topic       TEXT NOT NULL,
    language    TEXT,
    handled     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bookings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    call_sid    TEXT,
    name        TEXT NOT NULL,
    phone       TEXT NOT NULL,
    slot_date   TEXT NOT NULL,
    slot_time   TEXT NOT NULL,
    location    TEXT,
    topic       TEXT,
    UNIQUE (slot_date, slot_time)
);
CREATE TABLE IF NOT EXISTS calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    call_sid    TEXT,
    caller_id   TEXT,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    outcome     TEXT
);
CREATE TABLE IF NOT EXISTS transcripts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    call_id     INTEGER,
    role        TEXT NOT NULL,
    text        TEXT NOT NULL
);
"""


@contextmanager
def connect():
    """Yield a row-dict connection, committing on clean exit."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # timeout: two calls finishing at once otherwise raise "database is locked"
    # instead of waiting. WAL: lets the dashboard read while a call writes.
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    """Create tables if they do not exist yet. Safe to call on every boot."""
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Databases created before Infinity's four branches were known lack this.
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(bookings)")}
        if "location" not in columns:
            conn.execute("ALTER TABLE bookings ADD COLUMN location TEXT")
        # Transcripts moved from call_sid to call_id. They are a convenience,
        # not a business record, so an old table is replaced rather than
        # migrated.
        transcript_columns = {r["name"] for r in conn.execute("PRAGMA table_info(transcripts)")}
        if transcript_columns and "call_id" not in transcript_columns:
            conn.execute("DROP TABLE transcripts")
            conn.executescript(SCHEMA)


# --- Writes ---------------------------------------------------------------


def add_lead(**fields: Any) -> int:
    """Record a callback request. Returns the new row id."""
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO leads (created_at, call_sid, caller_id, name, phone, topic, language)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                config.now().isoformat(timespec="seconds"),
                fields.get("call_sid"),
                fields.get("caller_id"),
                fields["name"],
                fields["phone"],
                fields["topic"],
                fields.get("language", "de"),
            ),
        )
        return cur.lastrowid


def add_booking(**fields: Any) -> int | None:
    """Reserve a slot. Returns None if that slot was taken in the meantime."""
    try:
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO bookings"
                " (created_at, call_sid, name, phone, slot_date, slot_time, location, topic)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    config.now().isoformat(timespec="seconds"),
                    fields.get("call_sid"),
                    fields["name"],
                    fields["phone"],
                    fields["slot_date"],
                    fields["slot_time"],
                    fields.get("location"),
                    fields.get("topic"),
                ),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        # UNIQUE(slot_date, slot_time) -- someone else got it first.
        return None


def start_call(call_sid: str | None, caller_id: str | None) -> int:
    """Record an answered call. Returns its row id, which identifies it later.

    Browser sessions have no call_sid, so the row id -- not the sid -- is what
    end_call addresses. Matching on a NULL sid would update every browser call
    ever made.
    """
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO calls (call_sid, caller_id, started_at) VALUES (?, ?, ?)",
            (call_sid, caller_id, config.now().isoformat(timespec="seconds")),
        )
        return cur.lastrowid


def end_call(call_id: int | None, outcome: str) -> None:
    """Close out one call with what it achieved, for the capture-rate figure."""
    if call_id is None:
        return
    with connect() as conn:
        conn.execute(
            "UPDATE calls SET ended_at = ?, outcome = ? WHERE id = ?",
            (config.now().isoformat(timespec="seconds"), outcome, call_id),
        )


def call_stats() -> dict:
    """Calls answered, and how many ended with a booking or a phone number.

    The capture rate is the number worth showing an owner who is losing calls:
    every uncaptured call is a student who rang and left no trace.
    """
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM calls").fetchone()["n"]
        captured = conn.execute(
            "SELECT COUNT(*) AS n FROM calls WHERE outcome IS NOT NULL AND outcome != ''"
        ).fetchone()["n"]
    return {
        "total": total,
        "captured": captured,
        "rate": round(100 * captured / total) if total else 0,
    }


def add_transcript_line(call_id: int | None, role: str, text: str) -> None:
    """Append one conversation turn, so the office can read back what was said.

    Keyed by call_id, not call_sid: browser calls have no sid, and matching on
    a NULL sid mixes every browser call's transcript together -- the same trap
    that corrupted the call outcomes.
    """
    with connect() as conn:
        conn.execute(
            "INSERT INTO transcripts (created_at, call_id, role, text) VALUES (?, ?, ?, ?)",
            (config.now().isoformat(timespec="seconds"), call_id, role, text),
        )


# --- Reads ----------------------------------------------------------------


def recent_leads(limit: int = 50) -> list[dict]:
    """Newest callback requests first."""
    with connect() as conn:
        rows = conn.execute("SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def recent_bookings(limit: int = 50) -> list[dict]:
    """Upcoming appointments, soonest first."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM bookings ORDER BY slot_date, slot_time LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def taken_slots() -> set[tuple[str, str]]:
    """(date, time) pairs already booked, so they are not offered twice."""
    with connect() as conn:
        rows = conn.execute("SELECT slot_date, slot_time FROM bookings").fetchall()
        return {(r["slot_date"], r["slot_time"]) for r in rows}


def transcript_for(call_id: int) -> list[dict]:
    """Every turn of one call, in order."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM transcripts WHERE call_id = ? ORDER BY id", (call_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# --- Availability ---------------------------------------------------------


def free_slots(within_days: int | None = None) -> list[dict]:
    """Bookable consultation slots that are in the future and not yet taken.

    Returns dicts of {date, time, weekday} ordered soonest-first.
    """
    within_days = within_days or config.BOOKABLE["book_days_ahead"]
    taken = taken_slots()
    now = config.now()
    out: list[dict] = []

    for offset in range(1, within_days + 1):
        day: date = (now + timedelta(days=offset)).date()
        weekday = config.WEEKDAY_NAMES[day.weekday()]
        for hhmm in config.BOOKABLE["slots"].get(weekday, []):
            iso = day.isoformat()
            if (iso, hhmm) in taken:
                continue
            out.append({"date": iso, "time": hhmm, "weekday": weekday})
    return out


# German hour words, because the prompt tells the bot to speak numbers as words
# and the model then often hands them back the same way.
_HOUR_WORDS = {
    "null": 0,
    "ein": 1,
    "eins": 1,
    "zwei": 2,
    "drei": 3,
    "vier": 4,
    "fuenf": 5,
    "fünf": 5,
    "sechs": 6,
    "sieben": 7,
    "acht": 8,
    "neun": 9,
    "zehn": 10,
    "elf": 11,
    "zwoelf": 12,
    "zwölf": 12,
    "dreizehn": 13,
    "vierzehn": 14,
    "fuenfzehn": 15,
    "fünfzehn": 15,
    "sechzehn": 16,
    "siebzehn": 17,
    "achtzehn": 18,
    "neunzehn": 19,
    "zwanzig": 20,
    "einundzwanzig": 21,
    "zweiundzwanzig": 22,
    "dreiundzwanzig": 23,
    "vierundzwanzig": 24,
}


def parse_spoken_time(text: str) -> str | None:
    """Map whatever the model calls a time onto 'HH:MM', or None if unclear.

    Accepts '16:00', '16.00', '1600', '16', '16 Uhr', '16 Uhr 30', '16h' and
    'sechzehn Uhr'. Returning None matters as much as parsing: an unrecognised
    time must produce "say that again", not a booking at the wrong hour and not
    a misleading "that slot is taken".
    """
    raw = (text or "").strip().lower()
    if not raw:
        return None

    # Strip the words that carry no information, keeping any digits around them.
    cleaned = raw.replace("uhr", " ").replace("h", " ").strip()

    numbers = re.findall(r"\d{1,4}", cleaned)
    if numbers:
        first = numbers[0]
        if len(first) == 4:  # '1600'
            hour, minute = int(first[:2]), int(first[2:])
        else:
            hour = int(first)
            minute = int(numbers[1]) if len(numbers) > 1 else 0
    else:
        word = re.sub(r"[^a-zäöü]", "", raw.replace("uhr", ""))
        if word not in _HOUR_WORDS:
            return None
        hour, minute = _HOUR_WORDS[word], 0

    if not (0 <= hour <= 24 and 0 <= minute < 60):
        return None
    return f"{hour % 24:02d}:{minute:02d}"


def parse_spoken_date(text: str) -> str | None:
    """Best-effort map of what a caller says to an ISO date.

    Handles 'heute', 'morgen', 'uebermorgen', a weekday name (next occurrence),
    'YYYY-MM-DD', and 'DD.MM.' / 'DD.MM.YYYY'. Returns None if unparseable --
    the caller is then asked to repeat rather than being booked into a guess.
    """
    raw = (text or "").strip().lower()
    today = config.now().date()

    if raw in ("heute", "today"):
        return today.isoformat()
    if raw in ("morgen", "tomorrow"):
        return (today + timedelta(days=1)).isoformat()
    if raw in ("uebermorgen", "übermorgen"):
        return (today + timedelta(days=2)).isoformat()

    for index, name in enumerate(config.WEEKDAY_NAMES):
        if raw.startswith(name.lower()):
            ahead = (index - today.weekday()) % 7 or 7
            return (today + timedelta(days=ahead)).isoformat()

    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m."):
        try:
            parsed = datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
        if fmt == "%d.%m.":
            parsed = parsed.replace(year=today.year)
            if parsed < today:
                parsed = parsed.replace(year=today.year + 1)
        return parsed.isoformat()

    return None
