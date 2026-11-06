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

## The deals page (2026-10-25)

`/deals` is a board: one column per open stage (spotted … listed), closed deals (sold table
with profit, margin and days to sell; dropped list) under a "show" toggle. Tiles at the top:
open deals (and how many are stuck), cars owned and the cash invested in them, expected
profit over open deals, realised profit, average days to sell, hit rate (sold ÷ sold+dropped).
Filter by owner; track a car straight from the page by pasting its id or link on this site.

Each card shows the photo, owner, days in stage (red and "stuck" past the per-stage limit in
`STALE_DAYS`), distance from the base, ask → offer → bought, expected or actual profit, the
checklist progress for bought cars, a one-line **next action** (`next_action`) and buttons to
move to the next stage or drop it. On a desktop a card can also be dragged onto the previous or
next column (native HTML drag and drop, posts to the same stage endpoint, then reloads); on
phones the buttons remain the way to move a deal.

The deal page has a clickable stage progress bar, the next action, a drop button that asks for
a reason (stored on the stage event), an owner handover select (logged in the history), and
one "Estimate vs actual" table where the actuals are typed in place: our offer (`offer_eur`,
used for the expected profit before buying), purchase, transport, BPM, fees, repairs (the ad's
rough repair estimate is shown as the estimate), other, listed and sale price, with a Δ column.
The pickup checklist, pickup trip, nearby deals/cars, car notes and history are folds.

**Expected cost** (`Deal.expected_cost_eur`): the actual total cost once the purchase is
logged, otherwise the frozen landed-cost estimate (our offer replacing the ask once made) plus
the ad's rough repair estimate. **Expected profit** (`expected_profit_eur`): actual profit when
sold, otherwise sale or listed price or the frozen NL value minus the expected cost. Both are
summed over open deals in the tiles and shown on every card.

**Reminders** (`app/deals/reminders.py`, daily 08:00 Amsterdam, notification kind `deal` to
the owner): stuck in a stage past `STALE_DAYS`; the ad of a not-yet-bought deal disappeared;
its asking price changed since tracking. Same-day repeats refresh the unread notification.

## Teams and board filters (2026-11-06)

**Who sees what.** `teams` (name, plan) and `users.team_id`. A user without a team, or on a
team with the **individual** plan, sees and owns only their own deals: the board, the deal
pages, the owner handover list and the "deal" line on a car page are all limited to them. On
the **team** plan every member sees the whole board and can hand deals to each other
(`app/teams.py`: `teammates`, `visible_user_ids`). A car can be tracked once; tracking a car
that someone outside your team already has answers 409. The migration put every existing user
on one team named "Carscan" with the team plan, so nothing changed for the current group.
There is no billing: the plan is a flag an admin sets.

**Admin.** `/users` gained a team column per user, a Teams table (change plan, delete; members
keep their accounts and deals) and an "Add a team" form. CLI: `carscan teams create NAME
[--plan individual|team]`, `carscan teams assign USER [TEAM]`, `carscan teams list`. The
account page tells each user their plan and who they share deals with.

**Filters and sort on `/deals`.** Person (only on a shared team, with a deal count per
person), stage, country of the car, site (when more than one), free text over make, model,
variant, city, country and the deal notes, and sort by last activity, expected profit, asking
price, longest in stage or country. The summary tiles follow the filter; "n of m deals" says
how much is hidden; show/hide closed keeps the filters. Drag and drop needs two adjacent
columns, so it does nothing while a single stage is selected.
