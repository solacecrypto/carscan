# Deal pipeline and calibration (phase 5)

A **deal** is a listing someone is actually working on. Track one from the listing page
("Track as deal"); the current user becomes the owner and the estimates of that moment are
frozen on the deal (`est_price_eur`, `est_landed_cost_eur`, `est_bpm_eur`,
`est_transport_eur`, `est_fees_eur`, `est_nl_value_eur`, `est_nl_asking_median_eur`,
`est_spread_eur`).

Stages: spotted → contacted → inspected → bought → imported → listed → sold, plus **dropped**.
Any stage can be set from the deal page; moving to bought/listed/sold fills the matching date
if it is empty. Every change is a `deal_events` row (who, when, what).

Alert triage stays separate (Claim / Contacted / Pass on Telegram and the site), but it feeds
the pipeline: "contacted" moves a spotted deal to contacted, "passed" drops a deal that is
still spotted or contacted. A short Telegram group message is sent when a deal is marked
bought or sold.

**Actuals** (whole euros): purchase price, transport, BPM, RDW & fees, repairs, other, listed
price, sale price; dates bought/listed/sold; notes. Derived: actual landed cost = purchase +
transport + BPM + fees; total cost adds repairs and other; profit = sale − total cost; days to
sell = sold − listed (or bought).

**Calibration** (`/calibration`, `carscan calibrate`) compares frozen estimates with actuals
over bought/sold deals: transport per country, BPM ratio, fees, negotiated discount, haircut
from NL asking median to real sale price, NL value vs sale, days to sell, profit vs estimated
spread. Each row shows n; fewer than 3 is flagged. It prints suggested `costs.yaml` values;
apply them by editing the file and running `carscan costs recompute` / `carscan values recompute`.
