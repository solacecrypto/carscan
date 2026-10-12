# Source: Gaspedaal (comparables)

Investigated 2026-10-12. Aggregator of dealer stock. Plain GET; the results page embeds a
schema.org `ItemList` JSON-LD with 100 `Car` items per page.

```
https://www.gaspedaal.nl/bmw/3-serie/benzine?bmin=2008&bmax=2014&srt=df-a&page=N
```

- `numberOfItems` is the total (880 for 3-serie petrol 2008–2014); pages via `page=N`.
- Per item: `name` ("BMW 3-serie - Touring 335i xDrive …"), `productionDate` (year),
  `bodyType`, `fuelType`, `mileageFromOdometer.value`, `vehicleTransmission`,
  `vehicleConfiguration` ("Benzine, 2979cc, 225kW, Automaat, …"), `offers.price`,
  `offers.seller.name` + `address.addressLocality` (dealers), `image`.
- No per-motor-type filter in the URL: 318i/320i are matched from `name`. Listings overlap
  with AutoScout24.nl/Marktplaats; the valuation dedups by (make, model, year, mileage, price).
- Items are dealer stock, so no private-seller data; dealer names are business data.
- Fixture: `tests/fixtures/gaspedaal/search_page1.html` (only the JSON-LD block kept).
