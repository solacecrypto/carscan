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


## Recency sweeps (2026-11-06)

Step 1 of the roadmap above, for AutoScout24.de and Kleinanzeigen. `carscan models sweeps
--owner <user>` creates one saved search with role `sweep` per brand in `models.yaml` and per
site ("Sweep · BMW · autoscout24_de"), every 15 minutes (`SWEEP_INTERVAL_MINUTES`). A sweep
asks for the brand's ads newest-first (years from the oldest model we track, the brand's fuel
when its models agree, a price cap only when every model has one), fetches at most
`SWEEP_MAX_PAGES` pages, and stops at the first page that holds only ads we already have
(`known_ids` in the params, filled by the worker; both scrapers honour it). It stores every ad
it sees, stamps the watchlist model on the ones we track (`watchlist.classify`: make, the model
line from the AutoScout24 URL or the Kleinanzeigen title, chassis code, engine designation,
year, fuel, body), fetches details and runs the Claude pass only for those, and never
deactivates anything: a sweep sees only the newest ads, so absence means nothing. The
per-model searches keep doing deactivation; untracked cars are valued by model name only and
show up for spotters and the free-text search. Sweeps never queue Telegram alerts
(`alerts_enabled=False`); spotters and favorites decide what deserves attention.

**Interval and bands.** `carscan models sweeps --every 720 --pages 20 --band-years 4` reshapes
the sweeps for a 12-hour rhythm: each brand is split into 4-year registration bands so that
no band yields more new ads than the site serves pages for (AutoScout24 stops at 20 pages of
20). Sweeps the new layout no longer produces are paused. Sweep frequency barely changes the
number of search pages per day (that is set by the inflow of new ads); it changes how fresh
the site is and how many requests hit a site per hour.

**Bytes.** `PoliteClient.bytes_received` counts what came over the wire (compressed), stored
per run in `scrape_runs.bytes_fetched`. `carscan scrape traffic [--days N]` prints bytes per
source and role with a monthly projection; the Status page shows the last 24 h per source and
a sweep table. These numbers are what a proxy plan would bill.
