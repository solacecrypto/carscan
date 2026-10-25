# English descriptions

Seller descriptions are machine-translated into English and stored on
`listings.description_en`; the original stays in `description` and is shown under
"Show original" on the detail page. Condition tags and Claude insights keep working on the
original text.

- Provider via `TRANSLATE_PROVIDER`: `google` (default; the public endpoint behind the
  Google Translate page, no key, polite 1.5–4 s pauses, stops for the run on the first
  error), `deepl` (set `DEEPL_API_KEY`; free keys end in `:fx`), or `off`.
- The worker job `translate_backlog` runs every 5 minutes and handles `TRANSLATE_PER_RUN`
  (default 50) descriptions, best spread first. By hand: `carscan translate run --limit N`.
- A changed description clears the translation so it is redone.

## On demand (default since 2026-10-25)

Every car page has a "Translate to English" button that translates the description, the
condition-tag evidence and the repair estimate in one go and stores the result, so a car is
translated once and instantly on request. The background job only runs with
`TRANSLATE_AUTO=true`; the keyless Google endpoint blocks bulk use, which made the backlog
crawl. A refused request shows an error on the page instead of silently stalling.
