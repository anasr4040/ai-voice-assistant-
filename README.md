# Fahrschule Infinity — AI Telefonassistenz

A German-speaking AI receptionist that answers the phone for a Hamburg driving
school. It answers questions, books consultation appointments, takes callback
requests, and hands callers to a human when they ask.

Built on [Pipecat](https://github.com/pipecat-ai/pipecat). Cascaded pipeline
(speech-to-text → LLM → text-to-speech) rather than a realtime speech-to-speech
model, because it costs roughly a fifth as much per minute and every stage is
swappable — which matters most for German voice quality.

> **The business data in `src/receptionist/config.py` is invented.** Prices,
> address and hours are plausible placeholders, not Infinity's real numbers.
> Replace them before the owner hears this, or the bot will confidently quote
> prices that do not exist. `uv run python tests/test_tools.py` lists what is
> still a placeholder.

## Quick start — browser, no phone number needed

```sh
uv sync
cp .env.example .env        # fill in DEEPGRAM_API_KEY, OPENAI_API_KEY, CARTESIA_API_KEY
uv run bot.py
```

Open <http://localhost:7860>, allow the microphone, and talk to it in German.
The office view is at <http://localhost:7860/office>.

## Quick start — a real phone call

1. `ngrok http 7860` and copy the hostname.
2. Buy a voice number in the Twilio console.
3. Point the number's "A call comes in" webhook at `https://<ngrok-host>/`
   (HTTP POST). The runner answers with the TwiML that opens the media stream.
4. Put `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` in `.env`.
5. `uv run bot.py -t twilio -x <ngrok-host>`
6. Call the number.

A German `+49 40` Hamburg number needs a Twilio regulatory bundle with proof of
a German address, which takes a few days to approve. Any number works for the
demo; swap it later without touching the code.

## What it can do

| | |
|---|---|
| Answer questions | Hours, address, licence classes, prices, what to bring to registration, theory lesson times — all from `config.py` |
| Book appointments | Free consultation slots only. Never double-books, never invents a slot |
| Take callbacks | Name, number, topic → SQLite, email, and the `/office` page |
| Transfer to a human | Redirects the live Twilio call to `TRANSFER_NUMBER`; falls back to a callback if unset |
| German + English | Deepgram `nova-3` with `language=multi` handles code-switching mid-sentence |

It deliberately **cannot** book individual driving lessons (those depend on
instructor availability) and refuses to answer on MPU, refunds, complaints and
licence transfers — those become callback requests. See `ESCALATE_TOPICS`.

## Layout

```
bot.py                      entry point
src/receptionist/
  config.py                 ← the file you edit: hours, prices, classes, slots
  prompts.py                system prompt, generated from config
  tools.py                  the 4 actions the bot can take
  bot.py                    the voice pipeline
  store.py                  SQLite: leads, bookings, transcripts
  notify.py                 email the office
  telephony.py              Twilio: caller lookup, call transfer
  dashboard.py              /office and /office.json
tests/test_tools.py         offline check of every action, no API keys needed
```

## Swapping providers

Everything is an env var; no code changes.

```sh
TTS_PROVIDER=elevenlabs     # more natural German, costs more
LLM_PROVIDER=google         # gemini-2.5-flash, cheapest
LLM_PROVIDER=anthropic      # claude-haiku-4-5
STT_LANGUAGE=de             # German only, if code-switching misfires
```

## Testing without spending money

```sh
uv run python tests/test_tools.py
```

Runs the booking, double-booking, lead-capture and transfer-fallback paths
against a temporary database. No STT, LLM or TTS calls.

## Before this goes live

- Replace every placeholder in `config.py` with real data.
- Set `DASHBOARD_TOKEN` — `/office` shows real people's phone numbers.
- Add a recording/AI disclosure to the greeting (GDPR). Germany requires
  consent before recording; the bot currently stores transcripts, not audio.
- Host inside the EU (Hetzner, Scaleway) and check each provider's DPA.
