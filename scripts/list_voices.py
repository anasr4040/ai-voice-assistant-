"""Print the German voices available on YOUR account, with their IDs.

Voice IDs are account- and catalogue-specific, so never copy one from a blog
post -- a wrong ID gives you a call with no audio and no obvious error.

    uv run python scripts/list_voices.py

Then paste the ID you like into .env as CARTESIA_VOICE_ID or ELEVENLABS_VOICE_ID.
"""

from __future__ import annotations

import asyncio
import os

import aiohttp
from dotenv import load_dotenv

load_dotenv(override=True)


async def elevenlabs() -> None:
    key = os.getenv("ELEVENLABS_API_KEY")
    if not key:
        print("\nElevenLabs: no ELEVENLABS_API_KEY set, skipping.")
        return

    print("\n=== ElevenLabs ===")
    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": key}
        ) as response:
            if response.status != 200:
                print(f"  API error {response.status}: {await response.text()}")
                return
            voices = (await response.json()).get("voices", [])

    print("  (eleven_flash_v2_5 speaks German with any of these)\n")
    for voice in voices:
        labels = voice.get("labels") or {}
        verified = voice.get("verified_languages") or []
        langs = {entry.get("language") for entry in verified if isinstance(entry, dict)}
        marker = " <- German verified" if "de" in langs else ""
        descriptors = ", ".join(str(v) for v in labels.values() if v) or "-"
        print(f"  {voice.get('voice_id')}  {voice.get('name'):<22} {descriptors}{marker}")


async def cartesia() -> None:
    key = os.getenv("CARTESIA_API_KEY")
    if not key:
        print("\nCartesia: no CARTESIA_API_KEY set, skipping.")
        return

    print("\n=== Cartesia (German voices) ===")
    headers = {"X-API-Key": key, "Cartesia-Version": "2024-11-13"}
    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://api.cartesia.ai/voices", headers=headers, params={"limit": "100"}
        ) as response:
            if response.status != 200:
                print(f"  API error {response.status}: {await response.text()}")
                return
            payload = await response.json()

    # The endpoint has returned both a bare list and a paginated object.
    voices = payload if isinstance(payload, list) else payload.get("data", [])
    german = [v for v in voices if str(v.get("language", "")).startswith("de")]

    if not german:
        print("  No German voices listed. Showing all, pick one and set language=de:")
        german = voices[:25]

    for voice in german:
        print(
            f"  {voice.get('id')}  {str(voice.get('name'))[:24]:<24} "
            f"lang={voice.get('language')}  {str(voice.get('description', ''))[:48]}"
        )


async def main() -> None:
    await elevenlabs()
    await cartesia()
    print("\nPaste the ID you want into .env, then: uv run bot.py\n")


if __name__ == "__main__":
    asyncio.run(main())
