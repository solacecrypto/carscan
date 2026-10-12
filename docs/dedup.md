# Cross-source dedup (phase 4)

None of the sources exposes a VIN on list pages, so the same car advertised on two sites is
matched heuristically (`app/storage/dedup.py`, `is_same_car`):

- different source, same country, same make and model (318i/320i), compatible body type
  (unknown body on either side is accepted),
- same first-registration year, and same month unless one side only knows the year
  (Marktplaats, Gaspedaal, Bytbil, some Kleinanzeigen/Subito ads),
- mileage within 2 % of each other (both must be known),
- price within 3 % after currency conversion.

If a VIN is ever present on both sides it decides alone.

After every deals run the new listings are checked against active listings from other
sources. The listing seen first stays **canonical**; the later one gets `duplicate_of_id`.
Duplicates are hidden from the listings page by default ("include duplicates" shows them),
the canonical card carries an "also on …" badge, the detail page links both ways, and
duplicates never trigger a second Telegram alert. `carscan dedup run` re-links everything
(after tuning the tolerances, for example).
