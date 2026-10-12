# Phase 4 source investigation (2026-10-12)

Probed with plain HTTPS GET and browser-like headers. "JSON" = structured data available
without a browser. Per-source files are added as each scraper is implemented.

| Source | Result | Data | Notes |
|---|---|---|---|
| AutoScout24.be | 200 | `__NEXT_DATA__` | Same platform as .de/.nl; `/nl/lst/bmw?cat=…&cy=B`. 43 E90/E91 318i/320i listings. |
| AutoScout24.it | 200 | `__NEXT_DATA__` | `cat` motor-type ids are not honoured on .it; use `/lst/bmw/serie-3?fuel=B&body=5,6&…&cy=I` (42 listings) and filter `vehicle.model` ∈ {318, 320} client-side. `motorTypeName` is null there. |
| 2dehands.be | 200 | JSON API | Identical to Marktplaats (`/lrp/api/search`, same categories/attributes). 26 results for 320i 2008–2014. |
| Kleinanzeigen.de | 200 | HTML | 27 ads/page in `article[data-adid]` with title, price ("8.900 € VB"), "142.200 km", "EZ 07/2012", postcode+city. Pagination via `/seite:N/` in the path. Filters in path: `autos.ez_i:2008,2014`, `autos.marke_s:bmw`, model via path segment. Private sellers common; no names on the list page. |
| Subito.it | 200 | `__NEXT_DATA__` | `pageProps.initialState.items.originalList[]` with `features` (`/price`, `/register_date`, `/mileage_scalar`, `/gearbox`, `/car_type`, `/fuel`, `/power`), `geo`, `urls.default`, `advertiser` (strip `phone`, `shopName` for private). Query `?q=bmw+320i&ys=2008&ye=2014`, pagination `&o=N`. 44 results. |
| Automobile.it | 200 | `__NEXT_DATA__` | `pageProps.apiResults.result.resultList[]` (20/page, `&p=N`), `details.registration` ("Maggio 2011"), `formattedKm`, `fuelEmissions`, `shift`, `formattedPrice`, `sellerType`, `location`. Model via slug `/bmw-serie_3`, years via `attr_m_year_min/max`; fuel filter id unknown → filter client-side. 1,867 Serie 3 2008–2014 all fuels (94 pages). |
| Bytbil.com | 200 | HTML | `/bil?Makes=BMW&Models=3-serien&ModelYearRange.From=2008&ModelYearRange.To=2014&Fuels=Bensin`; `li.result-list-item` with title, "2008 \| 13 500 mil \| ÅSTORP", price in SEK (mil = 10 km). Only 3 matching cars in Sweden today. |
| mobile.de | **403** | – | Akamai "Zugriff verweigert" on the first request. Needs a real browser session and most likely residential proxies. Not implemented. |
| Gocar.be | **403** | – | Cloudflare "Just a moment" challenge. Needs Playwright. Not implemented. |
| Blocket.se | 200 shell | – | Single-page app. The search BFF lives under `/mobility/search/api/…` but the exact route could not be found from the bundles, and the in-app browser is not allowed to open blocket.se. Needs one XHR capture in a browser (Chrome extension or Playwright). |
| Kvd.se / kvdbil | SPA | – | Client calls `https://api.public.kvd.se/auction/search`, which does not resolve from here (geo or private DNS). Needs a browser capture from Sweden/NL. Low volume (auctions). |

Currency: Swedish sources quote SEK; `app/normalize/money.py` gets a configurable SEK rate.
