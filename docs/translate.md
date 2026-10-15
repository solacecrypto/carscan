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
