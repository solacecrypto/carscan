# Notifications, stars and web push

- A **star** (`watches` row) follows a car: price moves, disappearance/return, spread crossing
  zero, notes and claims by others produce in-app notifications (`notifications`, `app/notify`).
- **Wanted models** (`subscriptions`) produce a notification for each new matching car.
- **Health** issues (`app/health`) go to every user and the admin chat.
- The **bell** in the nav polls the unread count every minute.
- **Web push** (`app/push.py`, `pywebpush`): run `carscan push keygen`, put the three values
  in `.env` / Railway variables on both services, redeploy. Users enable it per device on
  `/account`. iPhones need the site added to the home screen first (the manifest and icons
  are in `app/web/static`). Dead subscriptions (404/410 or 5 failures) are removed.

## Favorites page (2026-10-25)

`/favorites` is its own page rather than a filtered listing: tiles (favorites and how many are
still for sale, price drops with the total saved since adding, positive-return count with the
average spread, targets set and reached), filter chips (for sale, gone, price dropped,
positive return, with target), sorts (recently added, biggest drop, highest spread, lowest
price). Each row shows the price now against the price when the star was set (from
`price_history`), spread, landed cost, target with an inline field to set it, deal link,
notes count, who else favorited it, and buttons to track as a deal or remove. In the UI the
feature is called "Favorites"; in code it remains the `watches` table and the `starred`
filter.

## Catch-up page (2026-10-26)

`/digest` is now a personal catch-up computed on request (`app.digest.catch_up`), with a
24 h / 2 days / week window: six count tiles (new cars and how many are profitable, price
drops, favorites with news, wanted-model matches, deals needing attention, unread
notifications), then the best new cars by spread, price drops (profitable first), red flags on
new profitable cars, the user's favorites with what happened to them (from their
notifications in the window), their open deals with the next action and stuck/ad-gone marks,
new wanted-model matches, and the models whose median spread moved most in 7 days. The shared
Telegram morning digests (`store_digest`, 07:00) are unchanged and kept in a fold underneath.

## Wanted page, bulk favorites, CSV export (2026-10-27)

- `/wanted` shows, per wanted-model subscription of the viewer, the cars on the market right
  now that match it (active, not duplicates, same `app.notify.matches` rules as the alerts),
  best spread first, with counts (on the market, new today, positive spread, best spread) and
  an "open in listings" link that translates the criteria into listing filters. Linked from the
  wanted card on `/notifications` and the wanted tile on the digest.
- Listings page: the **select** button in the results header shows a checkbox on every card;
  **all** ticks the page, **★ favorite** / **☆ unfavorite** post the ids to
  `POST /listings/star-bulk` (`ids`, `op` add|remove, `next` same-site path) and return to the
  same page and filters. Favoriting an already favorited car is a no-op.
- **⤓ CSV** downloads the current filters and sort (every page, up to 2.000 rows) from
  `GET /listings.csv`: semicolon separated with a UTF-8 BOM so Excel opens it as columns
  straight away; one row per car with price, landed cost, NL value, spread, the ad URL and the
  carscan URL.
