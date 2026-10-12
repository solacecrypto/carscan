# Source: AutoScout24.nl (comparables)

Investigated 2026-10-12. Same Next.js platform as AutoScout24.de (see `autoscout24_de.md`):
plain GET, `__NEXT_DATA__` with the identical `pageProps.listings[]` shape, 20 per page.

- Use the make-only path `/lst/bmw?cat=...&fuel=B&body=5,6&fregfrom=2008&fregto=2014&cy=NL`.
  The model path `/lst/bmw/3-series` ORs a model-line filter into `cat` and lets diesels in.
- First saved search gives 237 NL listings over 13 pages (2026-10-12).
- Same GDPR scrubbing as .de. Dealer `companyName` kept.
- Fixture: `tests/fixtures/autoscout24_nl/search_page1.html`.
