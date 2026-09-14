"""A one-page office view of what the receptionist captured.

Mounted onto the Pipecat runner's own FastAPI app, so there is one process and
one port to run. Server-rendered HTML with no build step: the point is that the
owner can open a link during the demo and watch their call turn into a row.
"""

from __future__ import annotations

import os
from html import escape

from fastapi.responses import HTMLResponse, JSONResponse
from pipecat.runner.run import app

from receptionist import config, store

# Set DASHBOARD_TOKEN and the page requires ?token=... -- worth doing the moment
# this runs anywhere public, since these rows are real people's phone numbers.
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "")

STYLE = """
:root { color-scheme: light dark; --bg:#f7f7f8; --fg:#1a1a1a; --card:#fff;
        --line:#e3e3e6; --muted:#6b6b72; --accent:#1f6feb; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#16161a; --fg:#ececf1; --card:#1f1f25; --line:#32323a; --muted:#9a9aa3; }
}
* { box-sizing:border-box; }
body { margin:0; padding:24px 16px; background:var(--bg); color:var(--fg);
       font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif; }
.wrap { max-width:900px; margin:0 auto; }
h1 { font-size:20px; margin:0 0 4px; }
.sub { color:var(--muted); font-size:13px; margin-bottom:24px; }
h2 { font-size:15px; margin:28px 0 10px; display:flex; gap:8px; align-items:baseline; }
.count { color:var(--muted); font-weight:400; font-size:13px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
        overflow:hidden; }
.scroll { overflow-x:auto; }
table { border-collapse:collapse; width:100%; min-width:520px; }
th,td { text-align:left; padding:10px 14px; border-bottom:1px solid var(--line);
        font-size:14px; vertical-align:top; }
th { font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }
tr:last-child td { border-bottom:none; }
td.num { font-variant-numeric:tabular-nums; white-space:nowrap; }
.empty { padding:20px 14px; color:var(--muted); font-size:14px; }
.pill { display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px;
        background:color-mix(in srgb, var(--accent) 14%, transparent); color:var(--accent); }
a { color:var(--accent); }
"""


def _table(headers: list[str], rows: list[list[str]], empty: str) -> str:
    """Render one table, or a friendly empty state."""
    if not rows:
        return f'<div class="card"><div class="empty">{escape(empty)}</div></div>'
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f'<td class="num">{cell}</td>' for cell in row) + "</tr>" for row in rows
    )
    return (
        f'<div class="card scroll"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


@app.get("/office", include_in_schema=False)
async def office(token: str = ""):
    """The office view: appointments booked and callbacks requested, by phone."""
    if DASHBOARD_TOKEN and token != DASHBOARD_TOKEN:
        return HTMLResponse("<h1>404</h1>", status_code=404)

    store.init()
    bookings = store.recent_bookings()
    leads = store.recent_leads()

    booking_rows = [
        [
            escape(b["slot_date"]),
            escape(config.speak_time(b["slot_time"])),
            escape(b["name"]),
            escape(b["phone"]),
        ]
        for b in bookings
    ]
    lead_rows = [
        [
            escape(lead["created_at"].replace("T", " ")[:16]),
            escape(lead["name"]),
            escape(lead["phone"]),
            escape(lead["topic"]),
        ]
        for lead in leads
    ]

    status = "geoeffnet" if config.is_open() else "geschlossen"
    html = f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(config.BUSINESS["name"])} &ndash; Telefonassistenz</title>
<style>{STYLE}</style></head>
<body><div class="wrap">
  <h1>{escape(config.BUSINESS["name"])} &ndash; Telefonassistenz</h1>
  <div class="sub">{config.now():%d.%m.%Y, %H:%M} Uhr &middot;
    Buero <span class="pill">{status}</span></div>

  <h2>Termine <span class="count">{len(bookings)}</span></h2>
  {_table(["Datum", "Uhrzeit", "Name", "Telefon"], booking_rows, "Noch keine Termine gebucht.")}

  <h2>Rueckrufe <span class="count">{len(leads)}</span></h2>
  {
        _table(
            ["Eingegangen", "Name", "Telefon", "Anliegen"],
            lead_rows,
            "Noch keine Rueckrufe notiert.",
        )
    }

  <h2>Rohdaten</h2>
  <div class="card"><div class="empty">
    <a href="/office.json{f"?token={escape(token)}" if token else ""}">office.json</a>
    &mdash; dieselben Daten als JSON.
  </div></div>
</div></body></html>"""
    return HTMLResponse(html)


@app.get("/office.json", include_in_schema=False)
async def office_json(token: str = ""):
    """Same data as JSON, for wiring into anything else later."""
    if DASHBOARD_TOKEN and token != DASHBOARD_TOKEN:
        return JSONResponse({"error": "not found"}, status_code=404)
    store.init()
    return JSONResponse({"bookings": store.recent_bookings(), "leads": store.recent_leads()})
