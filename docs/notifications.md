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
