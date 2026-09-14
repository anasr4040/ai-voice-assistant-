"""Getting captured leads and bookings in front of a human.

Email via SMTP if it is configured, otherwise a loud log line. The bot never
fails a call because notification failed -- the row is already in SQLite and
visible on the dashboard, so delivery is best-effort on purpose.
"""

from __future__ import annotations

import asyncio
import os
import smtplib
from email.message import EmailMessage

from loguru import logger

from receptionist import config

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "")


def _send_email_sync(subject: str, body: str) -> None:
    """Blocking SMTP send. Called via a thread so it cannot stall the pipeline."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = SMTP_USER or f"receptionist@{config.BUSINESS['name']}"
    message["To"] = NOTIFY_EMAIL
    message.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
        smtp.starttls()
        if SMTP_USER:
            smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(message)


async def notify(subject: str, body: str) -> None:
    """Deliver a notification to the office. Never raises."""
    banner = f"\n{'=' * 60}\n{subject}\n{'-' * 60}\n{body}\n{'=' * 60}"
    logger.info(banner)

    if not (SMTP_HOST and NOTIFY_EMAIL):
        logger.warning("SMTP not configured -- notification logged only, not emailed.")
        return

    try:
        await asyncio.to_thread(_send_email_sync, subject, body)
        logger.info(f"Notification emailed to {NOTIFY_EMAIL}")
    except Exception as exc:
        # A dead SMTP server must not cost us the call.
        logger.error(f"Could not email notification: {exc}")


async def notify_lead(*, name: str, phone: str, topic: str, caller_id: str | None) -> None:
    """Tell the office someone wants a callback."""
    await notify(
        f"Rueckruf gewuenscht: {name}",
        f"Name:     {name}\n"
        f"Telefon:  {phone}\n"
        f"Anliegen: {topic}\n"
        f"Anrufer-Nummer laut Netz: {caller_id or 'unbekannt'}\n"
        f"Eingegangen: {config.now():%d.%m.%Y %H:%M} Uhr",
    )


async def notify_booking(
    *, name: str, phone: str, slot_date: str, slot_time: str, topic: str | None
) -> None:
    """Tell the office an appointment was just booked over the phone."""
    await notify(
        f"Neuer Termin: {name} am {slot_date} um {slot_time}",
        f"Name:     {name}\n"
        f"Telefon:  {phone}\n"
        f"Termin:   {slot_date} um {slot_time} Uhr "
        f"({config.BOOKABLE['duration_minutes']} Minuten)\n"
        f"Anliegen: {topic or '-'}\n"
        f"Gebucht:  {config.now():%d.%m.%Y %H:%M} Uhr",
    )
