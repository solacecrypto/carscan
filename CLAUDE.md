# EU Car Import Arbitrage Scanner

Private internal tool for a small group (3–5 users) flipping used cars in the Netherlands.
It scans foreign used-car marketplaces (DE, SE, BE, IT) for cars that can be bought, imported
to NL and resold at a profit. Not a SaaS: no billing, no signup, internal use only.

Delivered as a website: everything runs on one Linux VPS, users access it through a browser
(desktop and mobile) behind a shared login. No local installs for users.

## Roadmap (all six phases are built; see the status sections at the bottom)

**Deployed on Railway** (2026-10-14): services `carscan` (web, ROLE=web), `worker` (ROLE=worker),
`Postgres`; one image, `docker/entrypoint.sh` picks the role. Web auto-deploys from GitHub
`main`; the worker is deployed with `railway up --service worker` from this checkout. Commands
run inside the container with `railway ssh --service carscan -- carscan <cmd>`.

**Long-term direction** (agreed 2026-10-15, see `docs/watchlist.md`): first a watchlist of
models (`models.yaml`, `carscan models sync`), then recency sweeps per site/segment with
attention rules, a model catalogue, and capacity work (proxies, workers, Claude gating).

1. **Phase 1 (current):** one source (AutoScout24.de) → normalizer → Postgres → website with
   listings, filters and saved searches → Telegram group alerts → deployed on a VPS
2. **Landed-cost calculator:** purchase price + transport + BPM estimate + RDW fees, shown per
   listing and usable as a filter
3. **NL comparables scraper** (Marktplaats, AutoScout24.nl, Gaspedaal) → NL value estimate →
   spread score (NL value − landed cost), sortable and filterable
4. **More sources:** mobile.de, Kleinanzeigen, Blocket, Bytbil, Kvdbil, AutoScout24.be/.it,
   2dehands.be, Gocar.be, Subito.it, Automobile.it; cross-source dedup (VIN, else fuzzy match)
5. **Deal pipeline:** spotted → contacted → inspected → bought → imported → listed → sold, with
   actual costs and sale prices logged, later used to calibrate the estimates
6. **LLM pass** over listing descriptions (Claude API), multilingual flags: accident damage,
   timing chain replaced, service history, export-only, defects

Do not build ahead of the current phase. Leave clean extension points (e.g. currency hook,
scraper registry) but no speculative features.

## Stack

- Python 3.12, `httpx` + `selectolax`; Playwright only when a source requires it (and say so first)
- Postgres 16, SQLAlchemy 2.x + Alembic migrations, Pydantic models
- FastAPI serving server-rendered pages: Jinja2 + HTMX + Tailwind (CDN). One codebase, one
  language, no separate frontend build
- Background worker with APScheduler running the scrapers alongside the web app
- `python-telegram-bot` for alerts
- Docker Compose: `web` + `worker` + `postgres` + `caddy` (automatic HTTPS). Config via `.env`
- Single Linux VPS with a domain
- Auth: username/password accounts stored hashed, session cookies. No signup page; users are
  created via a CLI command
- `pytest`, with saved HTML/JSON fixtures for parser tests

## Architecture

```
app/
  scrapers/<source>.py   # one file per source, common interface, registered in a registry
  normalize/             # RawListing -> NormalizedListing, prices in EUR (currency hook for SEK later)
  db/                    # SQLAlchemy models, session, Alembic migrations
  storage/               # upsert listings, price history, first_seen/last_seen, deactivation
  alerts/                # Telegram bot: messages, inline buttons, callback handling
  web/                   # FastAPI app, routers, templates, static
  worker/                # APScheduler jobs
  cli.py                 # user management etc.
tests/fixtures/<source>/ # saved HTML/JSON used by parser tests
```

- Scraper interface: `search(params) -> list[RawListing]` and `parse(raw) -> NormalizedListing`
- Prefer a site's internal JSON (XHR endpoints, embedded `__NEXT_DATA__`, JSON-LD) over HTML
  parsing. Investigate and document what each source exposes (in `docs/sources/<source>.md`)
  before implementing
- Scraping, normalization, storage, alerting and web are separate modules with no cross-imports
  except through the normalized schema and the DB layer
- Adding a new source = one new scraper file + registration. No core changes

## Data model (phase 1)

- `users`: id, username, password_hash, telegram_username (nullable), created_at
- `listings`: id, source, source_listing_id (unique per source), url, make, model, variant,
  body_type, engine_code (nullable), fuel, gearbox, power_kw, first_registration (date),
  mileage_km, price_eur, vat_deductible (bool, nullable), co2_g_km (nullable), vin (nullable),
  seller_type (dealer/private), country, city/postcode, photos (json), description,
  first_seen, last_seen, is_active, raw (jsonb)
- `price_history`: listing_id, price_eur, seen_at
- `saved_searches`: id, name, owner_id, source params (json), filter criteria (json), active,
  alerts_enabled
- `alerts`: listing_id, saved_search_id, sent_at, telegram_message_id,
  status (new/claimed/passed/contacted), claimed_by (user)

## Phase 1 scope

**Scraping**
- AutoScout24.de scraper for the first saved search: BMW 3 Series E90 (sedan) and E91
  (touring), 318i / 320i petrol, first registration 2008–2014
- Run every 30 min; track first_seen / last_seen; mark delisted cars inactive; record price
  changes in `price_history`
- Handle pagination, dedup within the source, and deleted/sold listings gracefully

**Website**
- Login page; every other page requires login
- Listings: table/cards, newest first, filters (price, mileage, year, gearbox, seller type,
  country, "new in last X hours", "price dropped") and sorting
- Listing detail: photos, specs, price history, link to the original ad, alert status
  (claimed/passed/contacted + by whom) with the same claim/pass/contacted buttons as Telegram
- Saved searches: create, edit, toggle searches and their alerts from the UI
- Status page: last scrape time per source, errors, number of active listings
- Mobile-first: deals are checked from phones

**Alerts**
- Telegram group alert for (a) new matching listings and (b) price drops on matching listings.
  Message: photo, title, price, mileage, year, gearbox, location, seller type, link to the
  original ad and to the listing on our site
- Inline buttons Claim / Pass / Contacted. Presses update alert status (synced with the
  website) and edit the message to show who claimed it

## Constraints

- **Polite scraping:** low request rates, randomized delays, retries with backoff, realistic
  headers. Log it, and alert a separate admin Telegram chat, when a source starts blocking or
  its parser breaks
- **GDPR:** do NOT store private sellers' names, phone numbers or emails. Dealer business names
  are fine. Strip these in the normalizer before anything hits the DB, including the `raw` blob
- **No secrets in code.** `DATABASE_URL`, `SECRET_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
  `TELEGRAM_ADMIN_CHAT_ID`, `DOMAIN` live in `.env`. Always keep `.env.example` current
- **Security:** HTTPS only, hashed passwords (argon2/bcrypt), CSRF protection on forms,
  login rate limiting

## Working style / conventions

- Plan first, then implement in small verifiable steps
- Write parser tests against saved fixtures before relying on live requests
- Tell the user when a source needs Playwright or proxies instead of silently adding them
- Runnable locally with `docker compose up` for development
- Code style: `ruff` for lint + format, type hints everywhere, Pydantic for boundaries
- Money is stored as integer EUR (no floats). Mileage as integer km. Dates as `date`/`datetime`
  in UTC
- Never commit `.env`, fixtures may not contain personal data of private sellers
- At the end of phase 1: a README with step-by-step VPS deployment (fresh Ubuntu server → Docker
  → domain → running site), written for someone not used to server admin

## Phase 6 status (2026-10-13)

Claude pass over descriptions done: `app/insights/` (schema, `ClaudeAnalyzer` via the
official SDK with structured output + server-side fallback, service), `listing_insights`
table, run inside each deals scrape before alerts (`INSIGHTS_PER_RUN`) plus a 10-minute
backlog job, filters/badges/Telegram line/status counts, `carscan insights run`. Off without
`ANTHROPIC_API_KEY` (`docs/insights.md`). Tested only against a fake client so far.

## Phase 5 status (2026-10-12)

Deal pipeline done: `deals` + `deal_events` tables, `app/deals/` (service + calibration).
Stages spotted → contacted → inspected → bought → imported → listed → sold, plus dropped.
Estimates are frozen on the deal at creation (`est_*`), actuals logged on the deal page,
alert status contacted/passed advances/drops an existing deal, group Telegram message on
bought/sold (`notify_group`). `/deals`, `/deals/{id}`, `/calibration`, `carscan calibrate`.
Calibration only suggests costs.yaml values; nothing is applied automatically.

## Phase 4 status (2026-10-12)

Added sources (plain HTTP): `autoscout24_be`, `autoscout24_it` (client-side `models` filter),
`tweedehands_be`, `kleinanzeigen` (HTML), `automobile_it`, `bytbil` (SEK via `fx:` in
costs.yaml). `subito` is implemented and fixture-tested but **DataDome blocks non-browser TLS**;
`mobile.de`, `gocar.be`, `blocket.se`, `kvd.se` are not implemented (see
`docs/sources/phase4_investigation.md`). Every scraper declares `headers` (Accept-Language).
Cross-source dedup in `app/storage/dedup.py` (`docs/dedup.md`): `listings.duplicate_of_id`,
duplicates hidden by default and never alerted. `carscan searches seed-phase4`, `carscan dedup run`.

## Phase 3 status (2026-10-12)

NL comparables and spread done: scrapers `autoscout24_nl`, `marktplaats` (internal JSON API),
`gaspedaal` (JSON-LD), all plain HTTP. `SavedSearch.role` = deals | comparables, each with its
own `interval_minutes`; the scheduler runs due searches every 5 min. Comparables live in the
`comparables` table; `app/valuation/` computes `nl_value_eur` / `spread_eur` per listing after
every run (`docs/valuation.md`). Valuation parameters under `valuation:` in `costs.yaml`.

## Phase 2 status (2026-10-12)

Landed-cost calculator done: `app/costs/` (pure functions), parameters in `costs.yaml`, method
and sources in `docs/landed_cost.md`. Computed on upsert, stored on `listings`
(`landed_cost_eur`, `bpm_eur`, `cost_breakdown`), shown on cards/detail/Telegram, filterable
(`landed_max`) and sortable. Two tariff tables are marked VERIFY in `costs.yaml`; the user
should check them against the Belastingdienst tarievenlijst. `carscan costs recompute` after
editing the config.

## Phase 1 status (2026-10-12)

Implemented and tested: AutoScout24.de scraper (JSON from `__NEXT_DATA__`, no Playwright),
normalizer with GDPR scrubbing, storage with price history/deactivation, worker with
APScheduler, website (login, listings, detail with claim/pass/contacted, saved searches,
status), Telegram alerts with buttons and two-way status sync, README with VPS deployment.
Not yet verified: a real `docker compose up` (no Docker on the dev Mac) and a real Telegram
bot session; both need to be exercised on the VPS. Tests use `pgserver` (embedded Postgres).

Dev notes:
- Migrations: while nothing is deployed, the single initial migration is regenerated with
  `alembic revision --autogenerate` against an embedded Postgres instead of adding new ones.
- `tests/fixtures/` HTML is scrubbed of personal data; never commit raw downloads.
