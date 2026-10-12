# Landed-cost estimate (phase 2)

`landed cost = purchase price + transport + BPM + RDW and registration fees`

All parameters live in `costs.yaml`. After editing it run
`carscan costs recompute` (or `docker compose exec web carscan costs recompute`).
`carscan costs show <price> --first-registration 2012-06-01 --co2 158` prints a breakdown for
a hypothetical car. Code: `app/costs/`.

## Transport

Flat amount per seller country (`transport:`), assuming a self-drive pickup with export plates.
Phase 5 will replace these with calibrated actuals.

## BPM

Dutch rules for a **used** import (Belastingdienst, "Bpm afschrijving" page):

1. **Bruto BPM** is computed with a tariff. You may use the most favourable tariff that applied
   between (2 months before) the first registration and the RDW inspection date.
2. **Depreciation** with the *forfaitaire afschrijvingstabel*: base percentage for the age
   bracket plus a percentage per started month inside the bracket. The table is binding once
   chosen. A koerslijst or taxatierapport may give a lower value, which we do not model.
3. Cars first registered **17 years 10 months ago or more** owe no BPM (table reaches 100%).

### Why only the 2013–2015 tariffs are configured

Tariffs for 2008–2012 contain a percentage of the Dutch net catalogue price, which we do not
have per listing. From 2013 BPM is purely CO₂-based, and for petrol cars around 140–170 g/km
the 2013–2015 tariffs are also the lowest of the whole window, so using the cheapest of them
is both computable and (nearly) the right answer. Later tariffs (2016+) are always higher for
these cars because the brackets kept shrinking.

| Year | Fixed | Brackets (g/km → €/g) | Status |
|---|---|---|---|
| 2013 | 0 | ≤95: 0 · 96–140: 125 · 141–208: 148 · 209–229: 276 · >229: 551 | confirmed |
| 2014 | 0 | ≤88: 0 · 89–124: 105 · 125–182: 126 · 183–203: 237 · >203: 474 | per-gram amounts indexed from Kamerstuk 33007; **verify** |
| 2015 | 175 | 1–82: 6 · 83–110: 69 · 111–160: 112 · 161–180: 217 · >180: 434 | fixed part and first two brackets confirmed; higher brackets **verify** |

Bracket boundaries for 2011–2015 come from Kamerstuk 33007 nr. 3 (table 1).

### Depreciation table (Belastingdienst, 2026)

| Age | Base | Per extra month |
|---|---|---|
| 0–1 m | 0% | 12% |
| 1–3 m | 12% | 4% |
| 3–5 m | 20% | 3.5% |
| 5–9 m | 27% | 1.5% |
| 9–18 m | 33% | 1% |
| 1.5–2.5 y | 42% | 0.75% |
| 2.5–3.5 y | 51% | 0.5% |
| 3.5–4.5 y | 57% | 0.42% |
| 4.5–5.5 y | 62% | 0.42% |
| 5.5–6.5 y | 67% | 0.42% |
| 6.5–7.5 y | 72% | 0.25% |
| 7.5–8.5 y | 75% | 0.25% |
| 8.5–9.5 y | 78% | 0.25% |
| ≥ 9.5 y | 81% | 0.19% |

Worked example, 320i first registered 2014-10, 158 g/km, inspected 2026-10: age 145 months →
81% + 31 × 0.19% = 86.89%. Cheapest tariff 2015: 175 + 82×6 + 28×69 + 48×112 = €7,975 bruto.
Net BPM = 7,975 × 13.11% ≈ **€1,046**. The same car from 2010 pays ≈ €340; from 2008, €0.

### CO₂ input

BPM for these years uses the NEDC figure. AutoScout24 exposes it on the detail page (often in
the `wltp` fields even for pre-WLTP cars). When a listing has no value the estimator takes
`co2_fallback` for "make model" and marks the result as *CO₂ assumed*; the detail page and the
Telegram message show that.

Non-petrol cars get no BPM estimate (and therefore no landed cost) in phase 2.

## Fixed fees (`fixed_fees:`)

| Item | € | Source |
|---|---|---|
| RDW identificatie voertuig licht | 58.50 | Regeling tarieven Dienst Wegverkeer 2026, Stcrt. 2025-40185 |
| RDW inschrijving kentekenregister | 50.00 | same |
| RDW tenaamstelling | 13.10 | same |
| Kentekenplaten | ~40 | plate maker, not RDW |
| Recycling bijdrage | ~20 | ARN |
| Export plates + insurance (DE Kurzzeitkennzeichen) | ~120 | typical |

Used EU cars with an EU type approval only need identification plus the registration; the
€1,128 "individuele goedkeuring speciaal invoer" applies to non-EU cars only.

## Not included

VAT (all target cars are margin-scheme or private sales older than 6 months), motorrijtuigen-
belasting after registration, repairs, APK, and your own time.

## Sources

- https://www.belastingdienst.nl/wps/wcm/connect/nl/bpm/content/bpm-afschrijving-koerslijst-taxatierapport-forfaitaire-tabel
- Kamerstuk 33007 nr. 3 and nr. 9 (bracket boundaries 2011–2015, tariff development)
- https://nl.wikipedia.org/wiki/Belasting_van_personenauto%27s_en_motorrijwielen (2013 tariff)
- Staatscourant 2025 nr. 40185 (RDW tariffs 2026)
