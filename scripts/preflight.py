"""Check the demo will actually work, before the owner is on the line.

Every key is tested against the real API, because the failure mode we care
about is not a crash -- it is a call that connects and then sits in silence.

    uv run python scripts/preflight.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

load_dotenv(override=True)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from receptionist import config  # noqa: E402

OK, WARN, BAD = "  OK  ", " WARN ", " FAIL "
problems: list[str] = []
warnings: list[str] = []


def judge_google_model(wanted: str, payload: dict) -> tuple[str, str]:
    """Decide whether the configured Gemini model is usable on this account.

    A valid key says nothing about the model: Google retires model names for
    new accounts, and the first sign of it used to be a 404 in the middle of a
    call. Pure function so it can be tested without a key.

    Returns a (status, detail) pair ready for report().
    """
    usable = [
        m["name"].removeprefix("models/")
        for m in payload.get("models", [])
        if "generateContent" in (m.get("supportedGenerationMethods") or [])
    ]
    if not usable:
        return WARN, f"key valid; could not list models to verify {wanted}"
    if wanted in usable:
        return OK, f"model={wanted} available on this account"

    preferred = [m for m in usable if "flash" in m and "lite" not in m]
    suggestion = ", ".join((preferred or usable)[:3])
    return BAD, (
        f"model {wanted} is NOT available to this account. Set LLM_MODEL to one of: {suggestion}"
    )


def report(status: str, label: str, detail: str = "") -> None:
    print(f"[{status}] {label}{f' -- {detail}' if detail else ''}")
    if status == BAD:
        problems.append(label)
    elif status == WARN:
        warnings.append(label)


async def check_deepgram(session: aiohttp.ClientSession) -> None:
    key = os.getenv("DEEPGRAM_API_KEY")
    if not key:
        report(BAD, "Deepgram", "DEEPGRAM_API_KEY not set -- the bot cannot hear")
        return
    async with session.get(
        "https://api.deepgram.com/v1/projects", headers={"Authorization": f"Token {key}"}
    ) as response:
        if response.status == 200:
            model = os.getenv("DEEPGRAM_MODEL", "nova-3")
            language = os.getenv("STT_LANGUAGE", "multi")
            report(OK, "Deepgram", f"key valid, model={model}, language={language}")
            if language == "multi" and model != "nova-3":
                report(WARN, "Deepgram language", "language=multi needs DEEPGRAM_MODEL=nova-3")
        else:
            report(BAD, "Deepgram", f"key rejected ({response.status})")


VALID_PROVIDERS = ("openai", "xai", "grok", "google", "anthropic")


async def check_llm(session: aiohttp.ClientSession) -> None:
    provider = os.getenv("LLM_PROVIDER", "openai").lower()

    if provider not in VALID_PROVIDERS:
        report(
            BAD,
            "LLM provider",
            f"LLM_PROVIDER={provider!r} is not a provider. Use one of: "
            "openai, xai, google, anthropic. If the line looks like "
            "'LLM_PROVIDER=xai   LLM_MODEL=...', split it onto two lines.",
        )
        return

    # The most common misconfiguration: a provider selected whose key is blank
    # or commented out. It passes every offline check and fails on the call.
    key_for = {
        "openai": "OPENAI_API_KEY",
        "xai": "XAI_API_KEY",
        "grok": "XAI_API_KEY",
        "google": "GOOGLE_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }
    if not os.getenv(key_for[provider]):
        others = [name for prov, name in key_for.items() if prov != provider and os.getenv(name)]
        hint = (
            f" You did set {', '.join(sorted(set(others)))} -- "
            f"did you mean to select that provider instead?"
            if others
            else ""
        )
        report(
            BAD,
            "LLM provider",
            f"LLM_PROVIDER={provider} but {key_for[provider]} is empty or commented out.{hint}",
        )
        return

    if provider == "openai":
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            report(BAD, "OpenAI", "OPENAI_API_KEY not set")
            return
        async with session.get(
            "https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"}
        ) as response:
            report(
                OK if response.status == 200 else BAD,
                "OpenAI",
                f"model={os.getenv('LLM_MODEL', 'gpt-4o-mini')}"
                if response.status == 200
                else f"key rejected ({response.status})",
            )
        return

    if provider in ("xai", "grok"):
        key = os.getenv("XAI_API_KEY")
        if not key:
            report(BAD, "xAI", "XAI_API_KEY not set")
            return
        wanted = os.getenv("LLM_MODEL", "grok-4.20-non-reasoning")
        async with session.get(
            "https://api.x.ai/v1/models", headers={"Authorization": f"Bearer {key}"}
        ) as response:
            if response.status != 200:
                report(BAD, "xAI", f"key rejected ({response.status})")
                return
            payload = await response.json()
        available = [m.get("id") for m in payload.get("data", []) if m.get("id")]
        if not available:
            report(WARN, "xAI", f"key valid; could not list models to verify {wanted}")
        elif wanted in available:
            report(OK, "xAI", f"model={wanted} available on this account")
        else:
            fast = [m for m in available if "non-reasoning" in m or "fast" in m]
            report(
                BAD,
                "xAI",
                f"model {wanted} not on this account. Set LLM_MODEL to one of: "
                f"{', '.join((fast or available)[:3])}",
            )
        return

    if provider == "google":
        key = os.getenv("GOOGLE_API_KEY")
        if not key:
            report(BAD, "Google", "GOOGLE_API_KEY not set")
            return
        wanted = os.getenv("LLM_MODEL", "gemini-3.6-flash")
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
        async with session.get(url) as response:
            if response.status != 200:
                report(BAD, "Google", f"key rejected ({response.status})")
                return
            payload = await response.json()

        status, detail = judge_google_model(wanted, payload)
        report(status, "Google", detail)
        return

    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        report(BAD, "Anthropic", "ANTHROPIC_API_KEY not set")
        return
    async with session.get(
        "https://api.anthropic.com/v1/models",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
    ) as response:
        report(
            OK if response.status == 200 else BAD,
            "Anthropic",
            f"model={os.getenv('LLM_MODEL', 'claude-haiku-4-5')}"
            if response.status == 200
            else f"key rejected ({response.status})",
        )


async def check_tts(session: aiohttp.ClientSession) -> None:
    provider = os.getenv("TTS_PROVIDER", "cartesia").lower()

    if provider == "elevenlabs":
        key = os.getenv("ELEVENLABS_API_KEY")
        if not key:
            report(BAD, "ElevenLabs", "ELEVENLABS_API_KEY not set -- the bot cannot speak")
            return
        async with session.get(
            "https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": key}
        ) as response:
            if response.status != 200:
                report(BAD, "ElevenLabs", f"key rejected ({response.status})")
                return
            ids = {v.get("voice_id") for v in (await response.json()).get("voices", [])}
        chosen = os.getenv("ELEVENLABS_VOICE_ID", "")
        if not chosen:
            report(
                BAD, "ElevenLabs voice", "ELEVENLABS_VOICE_ID not set -- run scripts/list_voices.py"
            )
        elif chosen not in ids:
            report(BAD, "ElevenLabs voice", f"{chosen} is not on this account -- silent calls")
        else:
            report(OK, "ElevenLabs", f"voice {chosen} available")

        # Model names get retired here too.
        wanted_model = os.getenv("ELEVENLABS_MODEL", "eleven_flash_v2_5")
        async with session.get(
            "https://api.elevenlabs.io/v1/models", headers={"xi-api-key": key}
        ) as response:
            if response.status != 200:
                report(WARN, "ElevenLabs model", f"could not verify {wanted_model}")
                return
            models = [m.get("model_id") for m in await response.json()]
        if wanted_model in models:
            report(OK, "ElevenLabs model", wanted_model)
        else:
            usable = [m for m in models if m and "flash" in m] or [m for m in models if m]
            report(
                BAD,
                "ElevenLabs model",
                f"{wanted_model} unavailable. Try: {', '.join(usable[:3])}",
            )
        return

    key = os.getenv("CARTESIA_API_KEY")
    if not key:
        report(BAD, "Cartesia", "CARTESIA_API_KEY not set -- the bot cannot speak")
        return
    headers = {"X-API-Key": key, "Cartesia-Version": "2024-11-13"}
    async with session.get(
        "https://api.cartesia.ai/voices", headers=headers, params={"limit": "100"}
    ) as response:
        if response.status != 200:
            report(BAD, "Cartesia", f"key rejected ({response.status})")
            return
        payload = await response.json()
    voices = payload if isinstance(payload, list) else payload.get("data", [])
    ids = {v.get("id") for v in voices}
    chosen = os.getenv("CARTESIA_VOICE_ID", "")
    if not chosen:
        report(BAD, "Cartesia voice", "CARTESIA_VOICE_ID not set -- run scripts/list_voices.py")
    elif chosen not in ids:
        report(WARN, "Cartesia voice", f"{chosen} not in the first page of voices -- verify it")
    else:
        report(OK, "Cartesia", f"voice {chosen} available")


async def check_twilio(session: aiohttp.ClientSession) -> None:
    sid, token = os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN")
    if not (sid and token):
        report(WARN, "Twilio", "not configured -- browser demo only, no phone calls")
        return
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json"
    headers = {"Authorization": aiohttp.encode_basic_auth(sid, token)}
    async with session.get(url, headers=headers) as response:
        report(
            OK if response.status == 200 else BAD,
            "Twilio",
            "credentials valid" if response.status == 200 else f"rejected ({response.status})",
        )


async def check_email(session: aiohttp.ClientSession) -> None:
    """Whether captured leads will actually reach a human."""
    recipient = os.getenv("NOTIFY_EMAIL")
    resend_key = os.getenv("RESEND_API_KEY")

    if not recipient:
        report(WARN, "Lead email", "NOTIFY_EMAIL unset -- leads only on /office")
        return

    if resend_key:
        async with session.get(
            "https://api.resend.com/domains",
            headers={"Authorization": f"Bearer {resend_key}"},
        ) as response:
            if response.status not in (200, 201):
                report(BAD, "Resend", f"key rejected ({response.status})")
                return
            domains = (await response.json()).get("data") or []

        sender = os.getenv("RESEND_FROM", "onboarding@resend.dev")
        verified = [d.get("name") for d in domains if d.get("status") == "verified"]
        if sender.endswith("@resend.dev"):
            report(
                WARN,
                "Resend",
                f"key valid, sending as {sender} -- the shared test sender only "
                f"delivers to your own Resend account address. Fine for testing; "
                f"verify a domain to email anyone else.",
            )
        elif verified and sender.split("@")[-1] in verified:
            report(OK, "Resend", f"key valid, sending as {sender}")
        else:
            report(
                BAD,
                "Resend",
                f"RESEND_FROM={sender} is not on a verified domain. "
                f"Verified: {', '.join(verified) or 'none'}",
            )
        return

    if os.getenv("SMTP_HOST"):
        report(OK, "Email", f"SMTP configured, leads go to {recipient}")
    else:
        report(
            WARN,
            "Email",
            f"no RESEND_API_KEY and no SMTP_HOST -- leads reach {recipient} nowhere. "
            "Resend is the simpler path.",
        )


def check_config() -> None:
    unknown = [name for name in config.LOCATIONS if not config.LOCATIONS[name]["address"]]
    if unknown:
        report(WARN, "Branch addresses", f"street unknown for: {', '.join(unknown)}")
    else:
        report(OK, "Branch addresses", f"all {len(config.LOCATIONS)} branches have one")

    if not config.PRICES_CONFIRMED:
        report(WARN, "Prices", "not owner-confirmed -- the bot refuses to quote any price")
    else:
        report(OK, "Prices", "confirmed by the owner")

    report(
        OK if config.TRANSFER_NUMBER else WARN,
        "Transfer number",
        config.TRANSFER_NUMBER or "unset -- 'put me through' becomes a callback",
    )


async def main() -> int:
    if not Path(".env").exists():
        print("No .env found. Run: cp .env.example .env\n")
        return 1

    print("\nChecking every credential against the live API...\n")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        await asyncio.gather(
            check_deepgram(session),
            check_llm(session),
            check_tts(session),
            check_twilio(session),
            check_email(session),
        )
    check_config()

    print()
    if problems:
        print(f"{len(problems)} blocking problem(s): {', '.join(problems)}")
        print("The demo will not work until these are fixed.")
        return 1
    if warnings:
        print(f"Ready to run, with {len(warnings)} warning(s): {', '.join(warnings)}")
        return 0
    print("All checks passed. Run: uv run bot.py")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
