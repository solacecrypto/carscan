# Source: AutoScout24.de

Investigated 2026-10-12 in a real browser and with plain `curl`. Fixtures in
`tests/fixtures/autoscout24_de/` (personal data scrubbed, see bottom).

## Verdict

- **No Playwright, no proxies needed.** Plain HTTPS GET with realistic browser headers returns
  the full page including `__NEXT_DATA__`. Even a request without any User-Agent got a 200.
- **Data source: embedded `__NEXT_DATA__` JSON** on both the search results page and the
  listing detail page. No HTML parsing needed. JSON-LD only contains the Organization block.
- No cookie consent is required for the server-rendered data.
- No public JSON API was needed; the Next.js data is complete enough.

## Search page

URL pattern (the make/model-group path form, not the bare `/lst?`):

```
https://www.autoscout24.de/lst/bmw/3er
  ?cat=ma13gr100038ge25mt102,ma13gr100038ge25mt103
  &fuel=B&body=5,6&fregfrom=2008&fregto=2014
  &cy=D&atype=C&damaged_listing=exclude
  &sort=age&desc=1&page=N
```

Parameters (from `pageProps.taxonomy` and `pageProps.pageQuery`):

| Param | Meaning | Values used |
|---|---|---|
| `cat` | taxonomy filter: `ma<make>gr<modelgroup>ge<generation>mt<motortype>`, comma-separated OR | make 13 = BMW, group 100038 = 3er, generation 25 = E90/E91/E92/E93 (26 = F30/F31), motortype 102 = 318i, 103 = 320i |
| `body` | body type | 5 = Kombi (E91 Touring), 6 = Limousine (E90). 1 Kleinwagen, 2 Cabrio, 3 Coupé, 4 SUV, 12 Van, 13 Transporter |
| `fuel` | fuel | B petrol, D diesel, E electric, L LPG, C CNG, 2 petrol hybrid |
| `fregfrom` / `fregto` | first registration year | 2008 / 2014 |
| `cy` | country | D, A, B, NL, I, F, E, L; `A,B,D,E,F,I,L,NL` = all |
| `atype` | article type | C = car |
| `custtype` | seller type (optional) | D dealer, P private |
| `gear` | gearbox (optional) | A automatic, M manual, S semi |
| `sort` / `desc` | sorting | `age` + `desc=1` = newest first; also `price`, `year`, `mileage`, `power` |
| `page` | 1-based page | 20 listings per page |
| `damaged_listing` | | `exclude` |

Alternative without `cat`: `/lst/bmw/320/ft_benzin?...` (one model per path) or
`mmmv=13|1640||,13|1641||` (318 = model 1640, 320 = 1641), but those cannot filter by
generation, so F30 cars from 2012–2014 leak in. The `cat` form is what we use.

Result size for our first saved search (2026-10-12): 284 listings, 15 pages.
Pagination: `pageProps.numberOfResults`, `pageProps.numberOfPages`. A page beyond the last
returns HTTP 200 with `listings: []` (fixture `search_empty.html`). Stop when
`page > numberOfPages` or the listing array is empty. AutoScout24 historically caps results at
20 pages (400 listings); if a saved search exceeds that, split it (e.g. by price range or
sort direction). Not an issue for phase 1.

### Listing object on the search page (`pageProps.listings[]`)

Fields we use:

| Path | Example | Notes |
|---|---|---|
| `id` | UUID | `source_listing_id` |
| `url` | `/angebote/bmw-3er-320i-touring-benzin-cat_...-<uuid>` | prefix with origin |
| `price.priceRaw` | 5500 | integer EUR |
| `price.isVatLabelLegallyRequired` | bool | weak VAT hint; detail page has `prices.public.taxDeductible` |
| `vehicle.make` / `modelGroup` / `model` / `motorTypeName` / `variant` | BMW / 3er / 320 / 320i / Touring | variant is Limousine / Touring / Gran Turismo / Coupé / Cabrio |
| `vehicle.modelVersionInput` | "320i Touring" | seller-entered title |
| `vehicle.transmission` | Schaltgetriebe / Automatik | |
| `vehicle.fuel` | Benzin | |
| `vehicle.mileageInKm` | "273.749 km" | formatted; `tracking.mileage` = "273749" raw string |
| `tracking.firstRegistration` | "08-2008" | MM-YYYY |
| `tracking.modelTaxonomy` | "[make_id:13, ... generation_id:25, motortype_id:103, ...]" | parse generation if needed |
| `vehicleDetails[]` | `{ariaLabel:"Leistung", data:"125 kW (170 PS)"}` | kW parsed from here; CO₂ is usually placeholder |
| `location.city` / `zip` / `countryCode` | Neufahrn i.NB / 84088 / DE | |
| `seller.type` | `Dealer` / `PrivateSeller` | |
| `seller.id` | string | dealer id, fine to keep for dealers |
| `images[]` | `.../<img>.jpg/250x188.webp` | thumbnails; swap suffix for `/1280x960.webp` for full size |
| `isOfferNew` | bool | AS24's own "new" flag, not authoritative for us |

**Not on the search page:** description, CO₂ (placeholder), VIN, body type string,
equipment, `taxDeductible`, numeric first registration date, raw kW. Everything needed for the
list view and alerts IS there, so the plan is: scrape search pages every run, and fetch the
detail page **once per newly discovered listing** (and on demand) to enrich.

### Personal data on the search page (GDPR)

`seller.phones[]` contains private sellers' phone numbers (`phoneType: "Private"`). The
normalizer must drop `seller.phones`, `seller.contactName`, `whatsappNumber` and, for
private sellers, `location.street`/`latitude`/`longitude` **before** anything (including the
`raw` jsonb column) is written. Dealer `companyName` is kept.

## Detail page

URL: `https://www.autoscout24.de` + listing `url`. Data in
`__NEXT_DATA__.props.pageProps.listingDetails`. Fixture: `detail_dealer.html`.

| Path | Notes |
|---|---|
| `status` | `"Active"` on live listings (other values not yet observed) |
| `createdTimestampWithOffset` | ISO timestamp the ad was created |
| `description` | HTML string (`<br />`, `<strong>`), seller-written, may contain phone numbers → regex scrub |
| `images[]` | `/1280x960.webp` full-size URLs |
| `prices.public.priceRaw` / `.taxDeductible` / `.negotiable` / `.netPriceRaw` | integer EUR; `taxDeductible` → `vat_deductible` |
| `prices.public.median` / `evaluationRanges` | AS24's own price evaluation; store in raw, might be useful later |
| `vehicle.firstRegistrationDateRaw` | `"2011-08-01"` |
| `vehicle.mileageInKmRaw` | int |
| `vehicle.rawPowerInKw` / `rawPowerInHp` | int |
| `vehicle.transmissionType` | Automatik / Schaltgetriebe |
| `vehicle.bodyType` | Limousine / Kombi … ; `rawData.bodyType.raw` = `Sedan` / `StationWagon` (English) |
| `vehicle.fuelCategory.raw` | `B` |
| `vehicle.co2emissionInGramPerKmWithFallback.raw` | NEDC, often null; `vehicle.wltp.co2EmissionsCombinedWithFallback.raw` often filled (159) |
| `vehicle.modelGenerationId` / `motorTypeId` / `modelVariantId` | 25 / 102 / 105 |
| `vehicle.rawData.classification.modelGeneration.formatted` | `"E90/E91/E92/E93"` |
| `vehicle.rawData.condition.damage.accidentFree` / `hadAccident` | bool |
| `vehicle.hasFullServiceHistory`, `noOfPreviousOwners`, `nextVehicleSafetyInspection` (TÜV "09/2028") | |
| `vehicle.equipment.*[]` | categorized equipment list (German labels) |
| `vehicle.licensePlate`, `hsnTsn` | null in sample; **do not store licensePlate** |
| `seller.type`, `seller.companyName`, `seller.id`, `seller.links.*` | keep for dealers |
| `seller.phones[]`, `seller.contactName`, `whatsappNumber`, `location.street/latitude/longitude` | **strip** |
| `location.city` / `zip` / `countryCode` | keep |

**VIN is not exposed** anywhere in the JSON. `vin` stays null for this source; cross-source
dedup (phase 4) will need the fuzzy path for AS24 cars.

`engine_code` is not exposed either (only displacement/cylinders). Stays null.

## Deleted / sold listings

A request for a non-existent listing id returns **HTTP 301** to the model list page
(`/lst/bmw/3er`). Treat a 301/302 from `/angebote/...` as "listing gone". A listing that
disappears from the search results across one full run is marked inactive by the storage
layer anyway; the detail-page redirect is the confirmation for on-demand refreshes.

## Politeness / blocking

- Observed: 200 on every request, ~0.4 s response time, no challenge page, no consent gate.
- `robots.txt`: disallows the bare `/lst?` and `/lst/?` query search for `*`, and blocks
  the big AI crawlers (`GPTBot`, `ClaudeBot`, `CCBot`, …) entirely. The make/model path form
  `/lst/bmw/3er?...` and `/angebote/...` are not disallowed. We still keep volume minimal:
  one saved search = ~15 search pages per 30 min plus one detail fetch per *new* listing.
- Client settings for the scraper: 2–5 s randomized delay between requests, max 1
  concurrent request, exponential backoff on 429/5xx, give up after 3 retries, realistic
  Chrome UA + `Accept-Language: de-DE`.
- Breakage detection: `__NEXT_DATA__` missing, `pageProps.listings` missing on page 1, or
  HTTP 403/429 → log + admin Telegram alert, mark the run failed on the status page.

## Fixtures

| File | What |
|---|---|
| `search_page1.html` | page 1 of the first saved search, 20 listings, mixed dealer/private |
| `search_page2.html` | page 2, used for pagination tests |
| `search_empty.html` | page 30 of a 15-page search, `listings: []` |
| `detail_dealer.html` | detail page of a dealer 318i Limousine, 2011 |
| `deleted_listing_response.txt` | note describing the 301 for a missing listing |

Scrubbing applied with a script before saving: `seller.phones` emptied, `contactName` and
`whatsappNumber` nulled, private sellers' street/coordinates removed, phone-number patterns in
descriptions replaced, and bulky irrelevant blocks (`translations`, `userSession`,
`optimizelyResults`, `recommendations`, ads config) dropped from `__NEXT_DATA__`. The
surrounding HTML is unchanged, so the parser is tested against the real page structure.
