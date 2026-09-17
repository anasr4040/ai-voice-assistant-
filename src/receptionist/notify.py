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

import aiohttp
from loguru import logger

from receptionist import config

# Resend is preferred over SMTP: one HTTPS call, no app passwords, no port 587
# being blocked on a cafe network. Falls back to SMTP, then to logging.
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM = os.getenv("RESEND_FROM", "onboarding@resend.dev")
RESEND_API_URL = os.getenv("RESEND_API_URL", "https://api.resend.com/emails")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "")


async def _send_via_resend(subject: str, body: str) -> bool:
    """Post one email through Resend. Returns whether it was accepted.

    Never raises: a captured lead is already in SQLite and on /office, so
    delivery is best-effort and must not cost us the call.
    """
    payload = {
        "from": f"{config.BUSINESS['name']} <{RESEND_FROM}>",
        "to": [NOTIFY_EMAIL],
        "subject": subject,
        "text": body,
    }
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.post(RESEND_API_URL, json=payload, headers=headers) as response:
                if response.status in (200, 201, 202):
                    logger.info(f"Lead emailed to {NOTIFY_EMAIL} via Resend")
                    return True
                detail = (await response.text())[:300]
                logger.error(f"Resend rejected the email ({response.status}): {detail}")
                if response.status == 403 and RESEND_FROM.endswith("@resend.dev"):
                    logger.error(
                        "Resend's shared test sender only delivers to your own account "
                        "address. To email anyone else, verify a domain and set "
                        "RESEND_FROM to an address on it."
                    )
                return False
    except Exception as exc:
        logger.error(f"Could not reach Resend: {exc}")
        return False


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

    if not NOTIFY_EMAIL:
        logger.warning("NOTIFY_EMAIL not set -- notification logged only, not emailed.")
        return

    if RESEND_API_KEY:
        await _send_via_resend(subject, body)
        return

    if not SMTP_HOST:
        logger.warning(
            "Neither RESEND_API_KEY nor SMTP_HOST set -- notification logged only. "
            "Set RESEND_API_KEY for the simpler path."
        )
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
