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
