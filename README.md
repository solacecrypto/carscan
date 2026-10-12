# Car Scanner

Private tool that watches foreign used-car marketplaces, stores every matching listing, shows
them on a phone-friendly website and pings a Telegram group when something new or cheaper
appears. Phase 1 covers AutoScout24.de. See `CLAUDE.md` for the roadmap and
`docs/sources/` for what each marketplace exposes.

---

## 1. Running it on your own machine (development)

Requirements: Docker Desktop (or Docker Engine + Compose plugin) and git.

```bash
git clone <this repo> carscan && cd carscan
cp .env.example .env            # defaults work locally; leave the Telegram lines empty for now
docker compose up --build
```

The first start builds the image, starts Postgres, applies the database migrations and serves
the site on http://localhost:8000. The worker starts scraping right away and then every 30
minutes.

Create a login and the first saved search (in a second terminal):

```bash
docker compose exec web carscan users create quinten
docker compose exec web carscan searches seed --owner quinten
docker compose exec web carscan searches seed-comparables --owner quinten   # NL asking prices for valuation
```

Run the scraper by hand instead of waiting for the schedule:

```bash
docker compose exec worker carscan scrape run
```

Other useful commands:

```bash
docker compose exec web carscan users list
docker compose exec web carscan users set-password quinten
docker compose exec web carscan searches list
docker compose exec web carscan costs show 6000 --first-registration 2012-06-01 --co2 158
docker compose exec web carscan costs recompute      # after editing costs.yaml
docker compose exec web carscan values recompute     # NL value + spread
docker compose logs -f worker
```

Landed-cost parameters (transport per country, BPM tables, RDW fees) and the valuation
settings live in `costs.yaml`; methods are explained in `docs/landed_cost.md` and
`docs/valuation.md`.

### Without Docker (tests, quick hacking)

```bash
uv venv -p 3.12 && uv pip install -e ".[dev]"
.venv/bin/pytest            # starts an embedded Postgres for the tests, no setup needed
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

---

## 2. Deploying to a VPS (step by step)

This takes about 30 minutes. You need:

- a VPS running **Ubuntu 24.04** (1 vCPU, 1–2 GB RAM is plenty; e.g. Hetzner CX22 or a
  DigitalOcean basic droplet) and its root password or SSH key,
- a **domain or subdomain** you control, e.g. `cars.example.com`,
- a Telegram account.

Commands marked `local$` run on your own computer, `server$` on the VPS.

### 2.1 Point the domain at the server

In your domain provider's DNS settings add an **A record**: name `cars` (or `@` for a bare
domain), value = the server's IPv4 address, TTL default. DNS can take a few minutes to
propagate; Caddy needs this to obtain the HTTPS certificate.

### 2.2 First login and basic hardening

```bash
local$ ssh root@<server-ip>
```

Create a normal user, give it sudo, and enable the firewall (SSH, HTTP, HTTPS only):

```bash
server$ adduser deploy            # pick a password; answer the other questions with Enter
server$ usermod -aG sudo deploy
server$ ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw --force enable
server$ apt update && apt -y upgrade
```

If you logged in with an SSH key, copy it to the new user so you can log in as `deploy`:

```bash
server$ rsync --archive --chown=deploy:deploy ~/.ssh /home/deploy
```

From now on log in as `deploy`:

```bash
local$ ssh deploy@<server-ip>
```

### 2.3 Install Docker

```bash
server$ curl -fsSL https://get.docker.com | sudo sh
server$ sudo usermod -aG docker $USER
server$ exit                       # log out and back in so the group change applies
local$  ssh deploy@<server-ip>
server$ docker compose version     # should print a version
```

### 2.4 Get the code onto the server

If the repository is on GitHub/GitLab (private is fine, use a deploy key or token):

```bash
server$ git clone <repo-url> ~/carscan && cd ~/carscan
```

Or copy it from your computer:

```bash
local$ rsync -av --exclude .venv --exclude .git --exclude .env ./ deploy@<server-ip>:carscan/
server$ cd ~/carscan
```

### 2.5 Configure

```bash
server$ cp .env.example .env
server$ nano .env
```

Fill in:

| Variable | Value |
|---|---|
| `POSTGRES_PASSWORD` | a long random password (e.g. output of `openssl rand -hex 16`) |
| `DATABASE_URL` | same password as above, e.g. `postgresql+psycopg://carscan:<password>@postgres:5432/carscan` |
| `SECRET_KEY` | output of `openssl rand -hex 32` |
| `DOMAIN` | your domain, e.g. `cars.example.com` |
| `TELEGRAM_*` | see section 2.7; can stay empty for now |

Save with `Ctrl+O`, `Enter`, exit with `Ctrl+X`.

### 2.6 Start it

On the server use the production compose file **only** (the dev override exposes ports and
mounts source code, which you do not want there):

```bash
server$ docker compose -f docker-compose.yml up -d --build
server$ docker compose -f docker-compose.yml logs -f --tail=50
```

Within a minute Caddy obtains a certificate and `https://cars.example.com` shows the login
page. (If it does not, check that the DNS record points at the server and that port 80/443
are open.) Press `Ctrl+C` to stop following the logs; the containers keep running.

Create the accounts for your group and the first search:

```bash
server$ docker compose -f docker-compose.yml exec web carscan users create quinten --telegram-username quinten
server$ docker compose -f docker-compose.yml exec web carscan users create friend1 --telegram-username theirhandle
server$ docker compose -f docker-compose.yml exec web carscan searches seed --owner quinten
server$ docker compose -f docker-compose.yml exec web carscan searches seed-comparables --owner quinten
```

`--telegram-username` is the person's Telegram @handle (without the @). It is what links a
button press in the Telegram group to the right account.

The worker runs the first scrape immediately. After a couple of minutes the Status page shows
the run and the Listings page fills up.

Tip: make an alias so you don't have to type the long command every time:

```bash
server$ echo "alias dc='docker compose -f ~/carscan/docker-compose.yml'" >> ~/.bashrc && source ~/.bashrc
server$ dc ps
```

### 2.7 Telegram alerts

1. In Telegram, talk to **@BotFather** → `/newbot` → pick a name and a username. Copy the
   **token** it gives you into `TELEGRAM_BOT_TOKEN` in `.env`.
2. Create a Telegram **group** for the deals and add the bot to it. Make the bot an admin of
   the group (BotFather → `/setprivacy` → Disable also works, but admin is simplest).
3. Find the group's chat id: post any message in the group, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and look for
   `"chat":{"id":-100...}`. That negative number is `TELEGRAM_CHAT_ID`.
4. For error notifications create a second (private) group or just use your own chat with
   the bot: send the bot `/start` in a private chat, call `getUpdates` again and use that
   positive id as `TELEGRAM_ADMIN_CHAT_ID`. Leave it empty to send errors to the main group.
5. Restart the worker so it picks up the new settings:

```bash
server$ dc up -d worker
server$ dc logs -f worker        # should say telegram.polling
```

New listings and price drops now arrive as messages with **Claim / Contacted / Pass** buttons.
A press updates the status on the website too, and vice versa.

### 2.8 Day-to-day

| Task | Command |
|---|---|
| See what's running | `dc ps` |
| Logs | `dc logs -f --tail=100 worker` (or `web`, `caddy`, `postgres`) |
| Update to a new version | `cd ~/carscan && git pull && dc up -d --build` (migrations run automatically) |
| Scrape now | `dc exec worker carscan scrape run` |
| Recompute landed costs after editing `costs.yaml` | `dc exec web carscan costs recompute` (then `dc up -d --build` so the new file is in the image) |
| Add a user | `dc exec web carscan users create <name> --telegram-username <handle>` |
| Reset a password | `dc exec web carscan users set-password <name>` |
| Database backup | `dc exec postgres pg_dump -U carscan carscan \| gzip > backup-$(date +%F).sql.gz` |
| Restore a backup | `gunzip -c backup.sql.gz \| dc exec -T postgres psql -U carscan carscan` |
| Restart everything | `dc restart` |
| Stop everything | `dc down` (data is kept in Docker volumes) |

A simple nightly backup: `crontab -e` and add
`0 3 * * * cd ~/carscan && docker compose -f docker-compose.yml exec -T postgres pg_dump -U carscan carscan | gzip > ~/backup-$(date +\%F).sql.gz`.

### 2.9 If something is wrong

- **Site unreachable / certificate error**: `dc logs caddy`. Usually the DNS record is not
  live yet, or `DOMAIN` in `.env` has a typo. Fix and `dc up -d caddy`.
- **Login page works but "Internal Server Error"**: `dc logs web`. Most often a wrong
  `DATABASE_URL` password.
- **No listings appearing**: Status page → last run. `dc logs worker`. A `BLOCKED` or
  `PARSER BROKEN` message also goes to the admin Telegram chat; it means AutoScout24 changed
  something or is refusing our requests. The scraper does not retry aggressively on purpose.
- **Telegram buttons say "not linked"**: that person's Telegram handle is not set on their
  account. `dc exec web carscan users list` shows the handles; recreate the user with
  `--telegram-username` or ask them to check the spelling (handles are matched
  case-insensitively, without the @).

---

## 3. Project layout

```
app/scrapers/     one file per marketplace; registry in app/scrapers/base.py
app/normalize/    shared listing schema, currency hook, GDPR scrubbing
app/storage/      upserts, price history, first/last seen, deactivation, filters
app/alerts/       alert queue, Telegram sending/buttons, admin notifications
app/worker/       scheduler + scrape job; the bot loop lives in app/worker/main.py
app/web/          FastAPI routes, Jinja2 templates (Tailwind + HTMX), auth, CSRF, rate limit
app/db/           SQLAlchemy models; migrations in migrations/
app/cli.py        `carscan` admin commands
tests/            pytest; parser tests run against saved fixtures in tests/fixtures/
docs/sources/     what each marketplace exposes and how the scraper uses it
```
