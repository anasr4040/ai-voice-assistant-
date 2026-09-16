# Deploying, and getting a number you can call

## Why not Vercel

Vercel cannot run this, and it is not a configuration problem.

A phone call through Twilio is **one WebSocket held open for the entire
conversation**. Twilio streams 8 kHz audio up it, we stream synthesised speech
back down, continuously, for as long as the caller is on the line. That needs a
process that stays alive and keeps per-connection state in memory.

Vercel functions are the opposite of that by design:

| What a call needs | What Vercel functions give |
|---|---|
| A WebSocket open for minutes | Request/response only; no inbound WebSocket server |
| A process alive between packets | Invoked per request, frozen in between |
| In-memory state for the call | Nothing survives an invocation |
| A writable database file | Read-only filesystem apart from `/tmp`, wiped constantly |
| Predictable audio latency | Cold starts measured in seconds |

Same for Netlify, Cloudflare Workers and plain Lambda. The correct shape here is
a small always-on container. The good news is that this is *cheaper* than
serverless for a workload that runs 24/7 at low volume.

If you want a Vercel-hosted marketing page for the Fahrschule that links to the
demo, that is a fine use of Vercel — just not for the voice pipeline.

## Deploy to Fly.io

`Dockerfile` and `fly.toml` are in the repo. Frankfurt region: closest to
Hamburg, and the audio stays in the EU.

```sh
fly launch --no-deploy --copy-config --name fahrschule-infinity-receptionist
fly volumes create receptionist_data --region fra --size 1

fly secrets set \
  DEEPGRAM_API_KEY=... \
  GOOGLE_API_KEY=... \
  ELEVENLABS_API_KEY=... \
  ELEVENLABS_VOICE_ID=... \
  TWILIO_ACCOUNT_SID=... \
  TWILIO_AUTH_TOKEN=... \
  TRANSFER_NUMBER="+4940644217 00" \
  DASHBOARD_TOKEN=...

fly deploy
```

Then check it came up:

```sh
curl https://fahrschule-infinity-receptionist.fly.dev/status
```

**If you rename the app or add a custom domain, update `PUBLIC_HOSTNAME` in
`fly.toml`.** The runner uses it to tell Twilio where to open the WebSocket; if
it is wrong or missing, calls connect and then sit in silence.

Cost: roughly €5/month for an always-on `shared-cpu-1x` with 1 GB. It measures
~132 MB idle, so 512 MB probably works and costs less — 1 GB just removes the
chance of an out-of-memory kill mid-demo.

`auto_stop_machines` is off deliberately. A machine scaled to zero cannot answer
a call: Twilio gives up long before a cold start finishes.

The same `Dockerfile` works on Railway, Render and any VPS, so nothing here
locks you to Fly.

## Pointing a phone number at it

Once deployed, set your Twilio number's **A call comes in** webhook to:

```
https://fahrschule-infinity-receptionist.fly.dev/     (HTTP POST)
```

That is all. The server answers that webhook with the TwiML that opens the
media stream — no TwiML Bin to maintain:

```xml
<Response>
  <Connect>
    <Stream url="wss://fahrschule-infinity-receptionist.fly.dev/ws"></Stream>
  </Connect>
</Response>
```

## Which number, for calling from a German mobile

This is the one part with a real trade-off, because the fast option and the
right option are not the same.

### Option A — German +49 number (the right end state)

A proper Hamburg number. Your German mobile dials it as a normal local call, and
the owner sees a number that looks like his own business.

The catch: Twilio requires a **regulatory bundle** for German numbers — proof of
a German address, submitted and approved before the number can be used. That is
typically a few business days, and Infinity's own business address should
satisfy it. Requirements do change, so check what the Twilio console asks for at
purchase time rather than trusting this paragraph.

**Start this now if you want the owner calling a +49 number**, because the wait
is calendar time you cannot compress.

### Option B — a non-German number today (test immediately)

A US or UK Twilio number is instant, no bundle. Everything works identically —
the bot does not know or care what number it is reached on.

The cost lands on **the caller**: dialling a US number from a German mobile is
an international call, and German flat rates generally do not include it. Fine
for you making a handful of test calls; a bad idea for a demo where you hand the
owner a number and ask him to ring it.

### Option C — browser, no number at all (free, works right now)

```sh
uv run bot.py
```

Open the URL and talk. Identical conversation logic, identical prompt, identical
booking flow — the only thing missing is the phone network. For a first look at
whether the German sounds right, this tells you almost everything, at zero cost
and zero wait.

### Recommended order

1. **Today:** browser demo, confirm the voice and the conversation.
2. **Today:** deploy to Fly, point a cheap non-German number at it, make a few
   test calls yourself to check real phone audio.
3. **In parallel:** start the German regulatory bundle.
4. **When approved:** swap the +49 number onto the same deployment. One webhook
   field, no redeploy.

## Securing a public deployment

The moment this has a public URL, anyone who finds it can talk to the bot and
spend your API credit.

- Set `DASHBOARD_TOKEN`. `/office` shows real callers' phone numbers.
- Consider `PIPECAT_ALLOWED_ORIGINS` to stop other sites embedding the browser
  demo, and `PIPECAT_WEBSOCKET_AUTH` to require a shared secret on `/ws`.
- Set spending caps in the Deepgram, ElevenLabs and Google consoles. This is the
  one that turns a bad day into a small one.
