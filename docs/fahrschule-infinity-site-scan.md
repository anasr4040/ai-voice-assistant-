# Site scan — fahrschule-infinity.de

**Target:** https://fahrschule-infinity.de
**Scanned:** 2026-09-14
**Scope:** content & structure scan (business facts, site map) — the kind of
knowledge base an assistant would answer from.

> ## ⚠️ Read this before trusting any value below
>
> **The site was never actually fetched.** This session's environment has no
> general outbound web egress — the egress proxy answered `403` to `CONNECT`
> for `fahrschule-infinity.de`, and equally for `example.com` and
> `google.com`, so this is an environment-wide network policy, not a
> site-specific block.
>
> Everything below is reconstructed from **search-engine index summaries**,
> not from live page content. That means it is:
> - **second-hand** — no HTML, no DOM, no page text was read;
> - **possibly stale** — prices and promotions especially;
> - **incomplete** — no full page inventory, no `robots.txt`/`sitemap.xml`,
>   no tech-stack, SEO, performance or accessibility audit.
>
> Treat every figure as *to be verified against the live site* before it is
> used in anything user-facing.

---

## Business identity

| Field | Value | Confidence |
|---|---|---|
| Name | Fahrschule Infinity | High |
| Legal entity | **Conflicting** — "Fahrschule Infinity UG (haftungsbeschränkt)" and "Fahrschule Infinity GmbH" both appear | Low — see *Conflicts* |
| Sector | Driving school (Fahrschule), Hamburg, Germany | High |
| Head office | Bramfelder Straße 95, 22305 Hamburg (Barmbek) | Medium |
| Phone | 040 64421700 (040 / 644 217 00) | Medium |
| Email | info@fahrschule-infinity.de | Medium |
| Language | German | High |

**Social:** Instagram [@fahrschule_infinity](https://www.instagram.com/fahrschule_infinity/),
TikTok [@fahrschule_infinity](https://www.tiktok.com/@fahrschule_infinity).

## Locations

The site has a dedicated page per branch. Four branch pages were surfaced:

| Branch | Page | Address | Confidence |
|---|---|---|---|
| Barmbek | `/fahrschule-hamburg-barmbek/` | Bramfelder Straße 95, 22305 Hamburg | Medium |
| Billstedt / Horn | `/fahrschule-hamburg-billstedt/` | not captured | — |
| Langenhorn (Langenhorn Markt) | `/fahrschule-hamburg-langenhorn/` | not captured | Low |
| Harburg (Phoenix-Center, basement level) | `/fahrschule-hamburg-harburg/` | Hannoversche Str. 86, 21079 Hamburg | Low |
| Bergedorf | *no page found* | — | Very low — see *Conflicts* |

## Opening hours

**Mon–Fri 13:00–19:00; Sat & Sun closed.** Medium confidence, and it is
unclear whether this applies to every branch or only to Barmbek — the two
sources that state it are the Barmbek directory listing and the Instagram
profile.

## Pricing (promotional — verify first)

| Package | Per driving lesson | Registration fee (Anmeldegebühr) | Included |
|---|---|---|---|
| **Infinity Standard** | 65,00 € | **395,00 €** (from 595,00 €) | All theory lessons, individual instructor coaching, **2 guaranteed driving slots/week** |
| **Infinity Plus** | 72,50 € | **495,00 €** (from 745,00 €) | Everything in Standard, plus **4 guaranteed driving slots/week** |

Discounts mentioned:
- Sign up together with a friend → **50 € off each** registration fee.
- Bring an invoice from a previous driving school → **145 € off** the
  registration fee.
- A blanket "**33 % off all registration fees**" campaign is also referenced.

These are struck-through promotional prices, so they are the values most
likely to be out of date. The 33 % campaign and the explicit
`395 €` / `495 €` figures may also be describing the same discount twice.

## Services

- Driving licence **class B** (cars up to 3,500 kg, incl. trailers up to 750 kg).
- **B197** — test taken on an automatic vehicle, with entitlement to drive
  manual cars afterwards.
- Full theory instruction (Theorieunterricht) included in both packages.
- Individual coaching by experienced instructors; guaranteed weekly slots.
- Motorcycle classes (A / A1 / A2 / AM) were **searched for but not
  confirmed** — do not assume they are offered.
- Theory-lesson timetables were not captured.

## Site map (URLs surfaced)

Main domain:
- `/` — home
- `/ueber-uns/` — about
- `/standorte/` — locations overview
- `/preise/` — prices
- `/kontakt/` — contact & directions
- `/online-anmeldung/` — online registration
- `/registrierung/` — registration
- `/fahrschule-hamburg-barmbek/`
- `/fahrschule-hamburg-billstedt/`
- `/fahrschule-hamburg-langenhorn/`
- `/fahrschule-hamburg-harburg/`

Subdomains:
- `karriere.fahrschule-infinity.de` — careers (hiring driving instructors);
  its Impressum lives at `?page_id=247`, suggesting **WordPress**
- `bewerbung.fahrschule-infinity.de` — application form ("no cover letter or
  CV required")

Not a complete inventory — only pages the search index surfaced.

## Conflicts to resolve

1. **Legal form.** Business directories list *Fahrschule Infinity GmbH*;
   the site's own imprint text is reported as *Fahrschule Infinity UG
   (haftungsbeschränkt)*. Plausibly a UG that later converted to a GmbH, in
   which case one source is simply stale. The imprint is authoritative.
2. **Branch count.** "4 locations" appears on the site's own pages; one
   summary claims 5 and adds Bergedorf. Only 4 branch pages exist, so
   Bergedorf is either new, closed, or an error.
3. **Which hours belong to which branch** — see *Opening hours*.

## To finish this scan properly

A real scan needs network access to the host. Once outbound egress is
allowed for this environment:

1. Fetch `/`, `/preise/`, `/standorte/`, `/kontakt/`, the four branch pages,
   and the imprint, and replace every value above with the page text.
2. Pull `robots.txt` and `sitemap.xml` for the true page inventory.
3. Capture the per-branch address / phone / hours table and the
   theory-lesson timetable.
4. Confirm the licence classes actually offered.

Until then, nothing here should be quoted to an end user as fact.

## Sources

- https://fahrschule-infinity.de/ · [/ueber-uns/](https://fahrschule-infinity.de/ueber-uns/) · [/standorte/](https://fahrschule-infinity.de/standorte/) · [/preise/](https://fahrschule-infinity.de/preise/) · [/kontakt/](https://fahrschule-infinity.de/kontakt/) (via search index)
- https://www.hamburg.de/branchenbuch/hamburg/eintrag/101039081/
- https://firmeneintrag.creditreform.de/22305/2152074737/FAHRSCHULE_INFINITY_GMBH
- https://www.instagram.com/fahrschule_infinity/
- https://karriere.fahrschule-infinity.de/

**Not the same business:** `infinityfahrschule.de` is a *different* driving
school that search results mix in. Nothing from that domain was used here.
