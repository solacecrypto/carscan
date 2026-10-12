# Source: Marktplaats (comparables)

Investigated 2026-10-12. Plain GET to the site's own JSON search API, no browser needed:

```
https://www.marktplaats.nl/lrp/api/search
  ?l1CategoryId=91            # Auto's
  &l2CategoryId=96            # BMW
  &attributesByKey[]=model:3-Serie
  &attributesByKey[]=fuel:Benzine
  &attributeRanges[]=constructionYear:2008:2014
  &query=320i                 # free text; motor type is not a structured attribute
  &limit=30&offset=0&sortBy=SORT_INDEX&sortOrder=DECREASING
```

- Response: `listings[]`, `totalResultCount`, `maxAllowedPageNumber` (27 → max ~800 results).
- Per listing: `itemId`, `title`, `priceInfo.priceCents` + `priceType` (FIXED, MIN_BID,
  SEE_DESCRIPTION, …; only FIXED/MIN_BID are usable prices), `attributes[]` with keys
  `constructionYear`, `fuel`, `transmission` (Automaat/Handgeschakeld), `body` (Sedan,
  Stationwagon, Coupé, Cabriolet, …), `mileage`, `model`; `vipUrl`, `pictures[]`, `date`,
  `location.cityName`, `sellerInformation.sellerId` (dealer when `showWebsiteUrl`/`isVerified`
  hint, otherwise unknown; we treat all as unknown seller type).
- Motor type (318i/320i) is matched from the title with a regex because the query is fuzzy.
- Strip `sellerInformation.sellerName`, coordinates, and phone numbers in descriptions.
- Fixture: `tests/fixtures/marktplaats/search_page1.json` (facets removed, names stripped).
