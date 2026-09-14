"""Twilio REST helpers: who is calling, and handing the call to a human.

Transfer works by replacing the TwiML on the live call. That tears down our
media stream and Twilio dials the office instead, so it is a cold transfer --
the caller does not hear us brief anyone. For a demo that is the right
trade-off: it is one API call and it cannot half-fail into dead air.
"""

from __future__ import annotations

import os
from xml.sax.saxutils import escape

import aiohttp
from loguru import logger

ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")

API_ROOT = "https://api.twilio.com/2010-04-01"


def _auth() -> aiohttp.BasicAuth | None:
    """Basic auth for the Twilio REST API, or None if unconfigured."""
    if not (ACCOUNT_SID and AUTH_TOKEN):
        return None
    return aiohttp.BasicAuth(ACCOUNT_SID, AUTH_TOKEN)


async def caller_number(call_sid: str | None) -> str | None:
    """The caller's number for this call, or None if it cannot be looked up.

    Used to pre-fill the callback number so the caller does not have to read
    their own number out to a machine.
    """
    auth = _auth()
    if not (call_sid and auth):
        return None

    url = f"{API_ROOT}/Accounts/{ACCOUNT_SID}/Calls/{call_sid}.json"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, auth=auth) as response:
                if response.status != 200:
                    logger.warning(f"Twilio call lookup failed: {response.status}")
                    return None
                return (await response.json()).get("from")
    except Exception as exc:
        logger.error(f"Twilio call lookup error: {exc}")
        return None


async def transfer_call(call_sid: str | None, to_number: str, say_first: str = "") -> bool:
    """Redirect a live call to `to_number`. Returns whether Twilio accepted it.

    `say_first` is spoken by Twilio (not by our TTS) while the office rings,
    because our pipeline is gone the moment the TwiML is replaced.
    """
    auth = _auth()
    if not (call_sid and auth and to_number):
        logger.warning("Transfer not possible: missing call_sid, credentials or target number.")
        return False

    say = f'<Say language="de-DE">{escape(say_first)}</Say>' if say_first else ""
    twiml = f"<Response>{say}<Dial>{escape(to_number)}</Dial></Response>"

    url = f"{API_ROOT}/Accounts/{ACCOUNT_SID}/Calls/{call_sid}.json"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, auth=auth, data={"Twiml": twiml}) as response:
                if response.status != 200:
                    logger.error(f"Transfer failed ({response.status}): {await response.text()}")
                    return False
                logger.info(f"Call {call_sid} transferred to {to_number}")
                return True
    except Exception as exc:
        logger.error(f"Transfer error: {exc}")
        return False
