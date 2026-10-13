# Claude pass over descriptions (phase 6)

Every active listing with a description gets one structured call to the Claude API
(`app/insights/`). The result is stored in `listing_insights` with a hash of the text, so a
car is analysed once and again only when the seller edits the ad. Failed calls are retried on
the next pass; refusals are not.

**Extracted fields** (any source language): `language`, `summary` (two English sentences),
`accident` (yes / no / unknown, with the quoted evidence), `timing_chain` (replaced / due /
not mentioned), `service_history` (full / partial / none / unknown), `export_only`, `defects`
(short English phrases). The prompt and JSON schema are in `app/insights/schema.py`.

**API use**: official `anthropic` SDK, model from `CLAUDE_MODEL` (default `claude-opus-5-5`),
structured output via `output_config.format` (always valid JSON), effort `low`, the system
prompt cached with `cache_control`, server-side refusal fallback (`fallbacks: "default"`). A
`refusal` stop reason or an API error is stored as a status and never fails a scrape run.
Roughly 600 input + 150 output tokens per listing: about half a cent each at Opus 5.5 prices.

**When**: inside every deals scrape run, after detail enrichment and before alerts are queued
(`INSIGHTS_PER_RUN` listings at most, newest first), so Telegram alerts carry the flags. A
backlog job runs every 10 minutes for whatever is still pending. `carscan insights run
--limit N` does it by hand. Without `ANTHROPIC_API_KEY` the feature is off and the status
page says so.

**Where**: badges on listing cards, a panel on the detail page with the evidence quote,
filters (hide accident cars, hide export-only, chain replaced only, full history only) on the
listings page and as alert criteria on saved searches, a 🔎 line in Telegram alerts, and
analysed/pending counts on the status page.

**Limits**: only what the seller wrote; a missing statement is `unknown`, never a guess.
Dealer boilerplate ("Fahrzeuge über 150.000 km nur für Export") is the main source of
`export_only`, which is exactly the trap it is meant to flag.
