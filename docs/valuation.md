# NL value estimate and spread (phase 3)

`spread = NL value − landed cost`

## Comparables

Saved searches with role **comparables** scrape Dutch asking prices into the `comparables`
table (no alerts, no detail pages). Defaults are created with
`carscan searches seed-comparables --owner <user>` (AutoScout24.nl, Marktplaats 318i and 320i,
Gaspedaal), running every 6 hours. Sources are documented in `docs/sources/`.

Marktplaats and Gaspedaal only expose the construction year, so `first_registration` is
stored as 1 January of that year (`raw.year_only = true`); the valuation only compares years.

## Estimate

For each active deal listing (`app/valuation/engine.py`):

1. Candidate pool: active comparables with the same model (318i/320i) and the same body type
   (sedan/estate), deduplicated across sites by (model, body, year, ~2,000 km, price).
2. Window: start with ±`year_window` years and ±`mileage_window_km`; widen stepwise up to
   `year_window_max` / `mileage_window_max_km` until at least `min_comps` match. Fewer than
   that → no estimate (shown as "not enough comparables").
3. Mileage adjustment: each comparable's asking price is moved by `km_rate_eur` per km of
   difference towards the target car's mileage.
4. NL value = median of the adjusted asking prices × (1 − `haircut_pct`/100). The interquartile
   range and the nearest comparables are stored in `listings.nl_value_breakdown` and shown on
   the detail page.
5. Spread = NL value − landed cost, also as a percentage of landed cost. Filter `spread_min`,
   sort "spread ↓", Telegram line "📈 NL value ≈ … · spread …".

Values are recomputed after every scrape run (deals or comparables) and with
`carscan values recompute`. Parameters live under `valuation:` in `costs.yaml`.

## Known limitations

- Asking prices, not sale prices: the haircut is a guess until phase 5 logs real sales.
- Private and dealer asking prices are pooled.
- Trim level, options, damage and colour are ignored; the nearest comparables list on the
  detail page is there so you can sanity-check an estimate before acting.
