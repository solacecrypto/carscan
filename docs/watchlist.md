# Watchlist (models.yaml)

The models we hunt live in `models.yaml`. One entry = one model, and `carscan models sync
--owner <user>` fans it out into the deal searches (AutoScout24.de/.be/.it, 2dehands,
Kleinanzeigen, Automobile.it, Bytbil) and the NL comparables searches (AutoScout24.nl,
Marktplaats). Searches are matched by name, so re-running is safe; a hand-made search with
identical parameters is adopted (renamed) rather than duplicated. `--prune` pauses watchlist
searches whose model was removed from the file; `--dry-run` prints what would be created.

Per entry: `label`, `make`, `years [from, to]`, `fuel` (petrol/diesel/…), `motors` (engine
names matched in titles where a site has no engine filter), `bodies` (sedan/estate/coupe/…),
and the site codes: AutoScout24 `path` + `cat` (taxonomy ids, see docs/sources/autoscout24_de.md;
`it_path` + `it_models` for Italy), Marktplaats `make_id` + `model`, Kleinanzeigen `make`,
Automobile.it `path`, Bytbil `make` + `model`. Leave a site out of `sources` to skip it.

Adding a model therefore needs one investigation per site to find its codes (ten minutes
with the browser), then one file edit and one command. Diesel entries get no BPM estimate
until the diesel tariff tables are added to `costs.yaml`.

## Spread gating

Two settings keep the expensive per-car steps proportional to opportunity:
`DETAIL_MIN_SPREAD_EUR` (detail pages, i.e. description/photos/CO₂ fetches) and
`INSIGHTS_MIN_SPREAD_EUR` (the Claude pass). A new car's spread is estimated from the search
page alone; only cars at or above the floor get a detail fetch and an analysis. Cars the
valuation cannot price yet (no comparables) always qualify, and a car that later drops in
price is picked up by the next run. Empty value = no gating. `price_max` on a watchlist entry
adds a price cap to the AutoScout24 queries (fewer pages).

## Roadmap after the watchlist

1. **Recency sweeps**: collect every new ad in a coarse segment (fuel, years, price band)
   per site and country, sorted newest-first, instead of per-model queries. Searches become
   *attention rules* (watchlist + spread threshold) evaluated over everything collected.
2. **Model catalogue**: canonical make/model/generation across sites (AutoScout24 taxonomy as
   the backbone) so valuation and dedup work for any model.
3. **Capacity**: per-site rate budgets, several workers, residential proxies for sites that
   block datacenter IPs (Gaspedaal, mobile.de), Claude gating on spread + batch API.
