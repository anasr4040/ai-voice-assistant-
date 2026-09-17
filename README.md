# Fahrschule Infinity — AI Telefonassistenz

A German-speaking AI receptionist that catches the calls Fahrschule Infinity
currently misses. It answers questions, books consultation appointments, takes
callback requests, and transfers to a human on request.

Built on [Pipecat](https://github.com/pipecat-ai/pipecat). Cascaded pipeline
(speech-to-text → LLM → text-to-speech) rather than a realtime speech-to-speech
model: roughly a fifth the cost per minute, and every stage swaps via env var —
which matters most for German voice quality.

> **The prices, address and hours in `src/receptionist/config.py` are invented
> placeholders.** Have the owner fill in [`docs/FRAGEBOGEN.md`](docs/FRAGEBOGEN.md)
> and transfer the answers first, or the bot will quote numbers that do not
> exist. `scripts/preflight.py` warns you while they are still placeholder.

## The problem this solves

Infinity loses calls when staff are teaching or already on the phone. Every
missed call is a prospective student who rang once and left no trace. So the
bot's single most important job is **not** sounding clever — it is never
letting a caller hang up without leaving a name and number. If a caller is
about to end the call with neither a booking nor a number, it asks exactly once,
then lets them go.

`/office` shows the number that matters: calls answered, and what share left
contact details.

## Installing

You do not install libraries one by one. The only prerequisites are:

| | |
|---|---|
| **Python 3.11+** | `python3 --version` |
| **uv** | `curl -LsSf https://astral.sh/uv/install.sh \| sh` (macOS/Linux) or `winget install astral-sh.uv` (Windows) |
| **Linux only** | `sudo apt install libgomp1` — needed by onnxruntime, which runs the voice-activity model. macOS and Windows already have it. |

Then one command installs everything, pinned by `uv.lock`:

```sh
uv sync
```

That pulls Pipecat (with the Deepgram, Google, ElevenLabs, WebRTC and Twilio
pieces), FastAPI, uvicorn and the rest — about 126 packages, ~860 MB. No
`pip install` list to maintain: every command below is `uv run ...`, which uses
that environment automatically.

## Setup (laptop)

```sh
cp .env.example .env
```

Fill in four keys — the file has signup links and explains each one:

| | Provider | Why this one |
|---|---|---|
| Hears | **Deepgram** | `nova-3` + `multi` handles German/English code-switching mid-sentence. Free signup credit covers a demo many times over |
| Thinks | **Google** `gemini-3.6-flash` | Free tier at aistudio.google.com, no card needed. Swap to OpenAI `gpt-4o-mini` or Anthropic `claude-haiku-4-5` with one env var |
| Speaks | **ElevenLabs** `eleven_flash_v2_5` | Best German of the affordable options. Switch to Cartesia (~half the price) once volume is real |
| Phones | **Twilio** | Any voice number. A German `+49 40` number needs a regulatory bundle and days of approval — demo on any number, swap later |

Then pick a voice from your own account and check everything works:

```sh
uv run python scripts/list_voices.py    # paste an ID into .env
uv run python scripts/preflight.py      # tests every key against the live API
```

`preflight` exists because the failure mode that ruins a demo is not a crash —
it is a call that connects and then sits in silence. It catches a wrong voice
ID, a rejected key and placeholder prices before the owner dials.

## Try it in a browser first

```sh
uv run bot.py
```

Open <http://localhost:7860>, allow the microphone, click **Connect**, and talk
to it in German. Office view: <http://localhost:7860/office>. No phone number
needed, nothing deployed.

**[docs/TESTSKRIPT.md](docs/TESTSKRIPT.md)** is a twelve-step German script —
what to say, what should happen — covering every capability including the ones
that must *not* fire (inventing a street, quoting a price, pushing twice for a
number). It doubles as the demo script for the owner call, and as a
troubleshooting table when something sounds wrong.

## Put it on a real phone

```sh
ngrok http 7860             # in a second terminal; copy the https hostname
uv run bot.py -t twilio -x <ngrok-host>
```

In the Twilio console, set your number's **A call comes in** webhook to
`https://<ngrok-host>/` (HTTP POST), then call the number.

ngrok's free tier gives you one reserved domain — use it
(`ngrok http --url=<your>.ngrok-free.app 7860`) so the Twilio webhook does not
need reconfiguring every restart.

Your laptop must stay awake and the two commands running for the number to
answer. That is fine for a scheduled demo call; it is not a deployment.

## Deploying

`Dockerfile` and `fly.toml` are in the repo — Frankfurt region, ~€5/month,
always on. See **[docs/DEPLOY.md](docs/DEPLOY.md)** for the walkthrough, the
choice of phone number for a German caller, and how to lock down a public URL.

**Vercel cannot host this.** A call is one WebSocket held open for the whole
conversation; Vercel functions are request/response, frozen between
invocations, with no writable disk. Same for Netlify, Cloudflare Workers and
Lambda. This needs a small always-on container, which for a 24/7 low-volume
workload is also the cheaper shape. DEPLOY.md has the detail.

## How it would actually run for Infinity

The owner keeps their existing number. They set **conditional call forwarding**
so only the calls they are already missing reach the AI:

| Condition | GSM code |
|---|---|
| No answer after ~20s | `*61*<twilio-number>#` |
| Busy | `*67*<twilio-number>#` |
| Cancel | `#61#` / `#67#` |

Those codes are standard on mobile. On a Hamburg landline it depends on the
provider — Telekom exposes the same feature as *Anrufweiterschaltung* in the
customer portal. Confirm with whoever supplies the line before promising it.

Nothing unconditional is forwarded, so staff answer exactly as they do today
and the AI only ever picks up calls that would have rung out.

## What it can and cannot do

| Can | |
|---|---|
| Answer questions | Hours, licence classes, prices, what to bring to registration, theory times — all from `config.py` |
| Book appointments | Free consultation slots only. Validated against real availability; a `UNIQUE` constraint makes double-booking impossible |
| Take callbacks | Name, number, topic → SQLite, email, `/office`. Auto-fills the caller's number from Twilio |
| Transfer | Redirects the live call to `TRANSFER_NUMBER` — only when asked, never offered, since the office being busy is why the AI answered |

It deliberately **cannot** book individual driving lessons (instructor-dependent)
and refuses to answer on MPU, refunds, complaints and licence transfers — those
become callback requests. See `ESCALATE_TOPICS` in `config.py`.

## Layout

```
bot.py                      entry point
src/receptionist/
  config.py                 ← the file you edit: hours, prices, classes, slots
  prompts.py                system prompt, generated from config
  tools.py                  the 4 actions the bot can take
  bot.py                    the voice pipeline
  store.py                  SQLite: calls, leads, bookings, transcripts
  notify.py                 email the office
  telephony.py              Twilio: caller lookup, call transfer
  dashboard.py              /office and /office.json
scripts/
  preflight.py              test every credential before a demo
  list_voices.py            German voices on your account, with IDs
tests/test_tools.py         every action, offline, no API keys
docs/FRAGEBOGEN.md          fill-in sheet for the owner
```

## One command before any demo

```sh
uv run python scripts/check.py
```

Stage 1 needs no API keys and costs nothing — it runs all three suites and
proves the code is sound. Stage 2 runs only if you have a `.env`, and tests
your keys against the live APIs without placing a call or synthesising a word.
It exits non-zero and tells you to stop if anything fails.

## Testing without spending money

```sh
uv run python tests/test_tools.py            # every action the bot can take
uv run python tests/test_prompt_delivery.py  # the prompt reaches the model
uv run python tests/test_pipeline_e2e.py     # a whole call, end to end, free
```

**`test_pipeline_e2e.py` is the one that answers "will a real call work".** It
runs the real pipeline — the real LLM service, the real adapter, Pipecat's real
tool-call parsing and dispatch, the real tools, a real database — against a
local server that speaks OpenAI's streaming protocol. Only the model's
judgement is faked, so every line of our own plumbing executes. It costs
nothing and needs no API keys.

It exists because unit tests on the tools cannot catch wiring bugs. It found
one immediately: `book_appointment` returned `location: "Barmbek"` while the
database column stayed NULL, because `add_booking` silently dropped the field.
The unit test asserted the tool's return value and passed.

The first covers booking, double-booking, unoffered slots, unknown branches,
lead capture and transfer fallback against a temporary database.

The second guards a failure that is completely silent: passing the system
prompt as a `"system"` message in `LLMContext` is deprecated since Pipecat 1.9
and is **dropped without warning** by the Google and Anthropic adapters. The
bot then answers fluently, in German, having never heard of the driving school,
and nothing in the logs says why. The prompt is set on the LLM service instead;
this test proves it arrives for all three providers.

## Rough running cost

About **€0.03–0.06 per minute** all-in, so ~300 calls × 3 min ≈ **€30–55/month**
plus hosting. A managed platform would be €90–135 for the same volume. These are
list-price estimates — verify against current pricing before quoting the owner.

## Before this handles real callers

- Transfer the questionnaire answers into `config.py`.
- Set `DASHBOARD_TOKEN` — `/office` shows real phone numbers.
- Move off the laptop (Hetzner ~€4/mo, EU hosting helps the GDPR conversation).
- Decide on the AI disclosure in the greeting, and check each provider's DPA.
  Transcripts are stored; audio is not.
