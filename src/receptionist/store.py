"""Persistence for everything the receptionist captures on a call.

SQLite on purpose: the demo has to survive a restart and be readable by the
office without anyone installing a database. Swap this module for the real
Fahrschul-Software API later; nothing else has to change.
"""

from __future__ import annotations

import os
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
    call_sid    TEXT UNIQUE,
    caller_id   TEXT,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    outcome     TEXT
);
CREATE TABLE IF NOT EXISTS transcripts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    call_sid    TEXT,
    role        TEXT NOT NULL,
    text        TEXT NOT NULL
);
"""


@contextmanager
def connect():
    """Yield a row-dict connection, committing on clean exit."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
                "INSERT INTO bookings (created_at, call_sid, name, phone, slot_date, slot_time, topic)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    config.now().isoformat(timespec="seconds"),
                    fields.get("call_sid"),
                    fields["name"],
                    fields["phone"],
                    fields["slot_date"],
                    fields["slot_time"],
                    fields.get("topic"),
                ),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        # UNIQUE(slot_date, slot_time) -- someone else got it first.
        return None


def start_call(call_sid: str | None, caller_id: str | None) -> None:
    """Record that a call the office would otherwise have missed was answered."""
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO calls (call_sid, caller_id, started_at) VALUES (?, ?, ?)",
            (call_sid, caller_id, config.now().isoformat(timespec="seconds")),
        )


def end_call(call_sid: str | None, outcome: str) -> None:
    """Close out a call with what it achieved, for the capture-rate figure."""
    with connect() as conn:
        conn.execute(
            "UPDATE calls SET ended_at = ?, outcome = ? WHERE call_sid IS ?",
            (config.now().isoformat(timespec="seconds"), outcome, call_sid),
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


def add_transcript_line(call_sid: str | None, role: str, text: str) -> None:
    """Append one conversation turn, so the office can read back what was said."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO transcripts (created_at, call_sid, role, text) VALUES (?, ?, ?, ?)",
            (config.now().isoformat(timespec="seconds"), call_sid, role, text),
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


def transcript_for(call_sid: str) -> list[dict]:
    """Every turn of one call, in order."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM transcripts WHERE call_sid = ? ORDER BY id", (call_sid,)
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
