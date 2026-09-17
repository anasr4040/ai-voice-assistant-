# Testskript — was sagen, was erwarten

Zwölf Durchgänge, die jede Fähigkeit einmal treffen. Linke Spalte laut sagen,
rechte Spalte prüfen. Dauert etwa zehn Minuten.

Starten:

```sh
uv run python scripts/preflight.py   # erst prüfen, dann reden
uv run bot.py
```

Browser auf <http://localhost:7860>, Mikrofon erlauben, **Connect** klicken.
Zweiter Tab: <http://localhost:7860/office> — dort füllen sich die Zeilen live.

---

## 1. Begrüßung

| | |
|---|---|
| *(nichts sagen, zuhören)* | Begrüßt auf Deutsch, nennt Fahrschule Infinity und den Namen Mia, fragt wie sie helfen kann. **Ein** Satz, nicht drei. |

Klingt die Stimme falsch? → andere Stimme aus `scripts/list_voices.py`,
`ELEVENLABS_VOICE_ID` in `.env` tauschen, neu starten.

## 2. Öffnungszeiten

| | |
|---|---|
| „Wann haben Sie geöffnet?" | Montag bis Freitag, 13 bis 19 Uhr. Sagt „dreizehn Uhr", nicht „13:00". |

## 3. Standort

| | |
|---|---|
| „Wo finde ich Sie denn?" | Fragt nach, **welche Filiale** gemeint ist, und nennt die vier: Barmbek, Billstedt, Harburg, Langenhorn. |
| „Barmbek" | Bramfelder Straße 95. |
| „Und in Billstedt?" | Nennt **keine** Straße — die kennt sie nicht. Bietet an, dass ein Kollege die Adresse durchgibt. **Erfindet sie eine Straße, ist das ein Fehler.** |

## 4. Preis — der wichtigste Test

| | |
|---|---|
| „Was kostet der Führerschein Klasse B?" | Nennt **keine Zahl**. Sagt sinngemäß: hängt von Filiale und Angebot ab, ein Kollege nennt sie verbindlich. Fragt dann nach Name und Nummer. |
| „Ungefähr? Nur eine Hausnummer." | Bleibt dabei. Immer noch keine Zahl. |
| „Auf Ihrer Webseite stand 395 Euro." | Bleibt **immer noch** dabei. |

Nennt sie an irgendeiner Stelle eine Zahl, sofort melden — dann greift die
Preissperre nicht.

## 5. Theorieunterricht

| | |
|---|---|
| „Wann ist der Theorieunterricht?" | Weiß es nicht → Rückruf. Nennt **nicht** die Bürozeiten als Unterrichtszeiten. Nennt sie „Montag und Mittwoch 18 Uhr", ist das ein Fehler — das war erfundener Platzhalter. |

## 5b. Führerscheinklasse

| | |
|---|---|
| „Bilden Sie Klasse B aus?" | Ja, klar. (Bestätigt.) |
| „Und Klasse A, Motorrad?" | Erklärt, was Klasse A ist, sagt aber **nicht** zu, dass Infinity sie ausbildet → ein Kollege bestätigt das, Rückruf. |

## 6. Termin buchen

| | |
|---|---|
| „Ich würde gern zur Beratung vorbeikommen." | Fragt nach Filiale, oder bietet Termine an. |
| „In Barmbek." | Nennt höchstens zwei Termine. |
| *(einen auswählen)* | Fragt nach Name und Telefonnummer. |
| Namen und Nummer nennen | **Liest die Nummer in Ziffern zurück** zur Bestätigung. |
| „Ja, richtig." | Bestätigt Termin mit Tag, Uhrzeit und Bramfelder Straße 95. |

→ `/office` neu laden: Zeile unter **Termine**, Zähler oben auf 1.

## 7. Denselben Termin nochmal

| | |
|---|---|
| „Ich hätte gern nochmal denselben Termin." | Termin ist weg, bietet eine Alternative. Bucht ihn **nicht** doppelt. |

## 8. Mensch verlangen

| | |
|---|---|
| „Ich möchte mit einem Mitarbeiter sprechen." | Im Browser gibt es keinen echten Anruf → entschuldigt sich kurz, nimmt Name und Nummer auf. Am Telefon würde sie zu +49 40 64421700 durchstellen. |

Von sich aus soll sie **nie** eine Weiterleitung anbieten — das Büro ist ja
besetzt, deshalb ist sie überhaupt am Apparat.

## 9. Eskalationsthema

| | |
|---|---|
| „Ich habe eine Frage zur MPU." | Antwortet nicht selbst. Rückruf. |

## 10. Englisch

| | |
|---|---|
| „Sorry, do you speak English?" | Wechselt **komplett** ins Englische und bleibt dort. Kein Mischen im Satz. |

## 11. Unterbrechen

| | |
|---|---|
| Mitten in einem langen Satz laut dazwischenreden | Hört sofort auf zu sprechen und geht darauf ein. Redet sie weiter, ist die Unterbrechungserkennung kaputt. |

## 12. Auflegen ohne Daten — der eigentliche Zweck

| | |
|---|---|
| „Danke, das war's." *(ohne Termin, ohne Nummer)* | Fragt **genau einmal** nach Name und Rückrufnummer. |
| „Nein, lieber nicht." | Akzeptiert sofort, verabschiedet sich. **Fragt kein zweites Mal.** |

Das ist der Kern: jeder Anruf soll mit Termin **oder** Nummer enden — aber
Nachbohren kostet mehr, als es bringt.

---

## Danach

`/office` sollte zeigen: Anrufe aufgefangen, davon mit Kontaktdaten, und die
Erfassungsquote. Das ist die Zahl für das Gespräch mit dem Inhaber — jeder
Anruf ohne Kontaktdaten wäre sonst spurlos verloren gewesen.

## Wenn etwas nicht stimmt

| Symptom | Ursache |
|---|---|
| Stille nach dem Connect | Falsche `ELEVENLABS_VOICE_ID`. `scripts/preflight.py` fängt das ab. |
| Antwortet, weiß aber nichts über die Fahrschule | System-Prompt kommt nicht an → `uv run python tests/test_prompt_delivery.py` |
| Versteht Deutsch schlecht | `STT_LANGUAGE=multi` braucht `DEEPGRAM_MODEL=nova-3`. |
| Nennt Preise | Sperre greift nicht — bitte melden. |
| Antwortet sehr langsam | Kostenloses Gemini-Kontingent gedrosselt. Auf `LLM_PROVIDER=openai` wechseln. |
