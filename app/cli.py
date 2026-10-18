"""Admin CLI. Inside the container: `docker compose exec web carscan --help`."""

import typer
from sqlalchemy import func, select

app = typer.Typer(no_args_is_help=True, help="Car Scanner admin commands")
users = typer.Typer(no_args_is_help=True, help="Manage login accounts")
searches = typer.Typer(no_args_is_help=True, help="Manage saved searches")
scrape = typer.Typer(no_args_is_help=True, help="Run scrapers")
costs = typer.Typer(no_args_is_help=True, help="Landed-cost estimates")
values = typer.Typer(no_args_is_help=True, help="NL value estimates")
dedup = typer.Typer(no_args_is_help=True, help="Cross-source duplicate detection")
app.add_typer(dedup, name="dedup")
app.add_typer(costs, name="costs")
app.add_typer(values, name="values")
app.add_typer(users, name="users")
app.add_typer(searches, name="searches")
app.add_typer(scrape, name="scrape")
translate = typer.Typer(help="English translations of descriptions")
app.add_typer(translate, name="translate")


@translate.command("run")
def translate_run(limit: int = typer.Option(50, help="max descriptions to translate")) -> None:
    """Translate descriptions that have no English text yet (best spread first)."""
    from app.worker.jobs import translate_backlog

    counts = translate_backlog(limit=limit)
    typer.echo(" ".join(f"{k}={v}" for k, v in counts.items()) or "translation is off")


# The first saved search of phase 1 (see docs/sources/autoscout24_de.md for the codes)
E90_PARAMS = {
    "path": "/lst/bmw/3er",
    "cat": "ma13gr100038ge25mt102,ma13gr100038ge25mt103",
    "fuel": "B",
    "body": "5,6",
    "fregfrom": 2008,
    "fregto": 2014,
    "cy": "D",
}


@app.command()
def version() -> None:
    """Print the application version."""
    from importlib.metadata import version as v

    typer.echo(v("carscan"))


@users.command("create")
def users_create(
    username: str,
    telegram_username: str | None = typer.Option(None, help="Telegram @handle, without the @"),
    password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=True),
) -> None:
    """Create a login account."""
    from app.db import User, db_session
    from app.web.security import hash_password

    if len(password) < 8:
        raise typer.BadParameter("password must be at least 8 characters")
    with db_session() as s:
        if s.scalar(select(User).where(User.username == username)):
            raise typer.BadParameter(f"user {username!r} already exists")
        s.add(
            User(
                username=username,
                password_hash=hash_password(password),
                telegram_username=telegram_username.lstrip("@") if telegram_username else None,
            )
        )
    typer.echo(f"created user {username}")


@users.command("set-password")
def users_set_password(
    username: str,
    password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=True),
) -> None:
    """Reset a user's password."""
    from app.db import User, db_session
    from app.web.security import hash_password

    with db_session() as s:
        user = s.scalar(select(User).where(User.username == username))
        if not user:
            raise typer.BadParameter(f"no such user {username!r}")
        user.password_hash = hash_password(password)
    typer.echo(f"password updated for {username}")


@users.command("list")
def users_list() -> None:
    """List accounts."""
    from app.db import User, db_session

    with db_session() as s:
        for u in s.scalars(select(User).order_by(User.id)):
            typer.echo(f"{u.id:>3}  {u.username:<20} telegram={u.telegram_username or '-'}")


@users.command("delete")
def users_delete(
    username: str, yes: bool = typer.Option(False, "--yes", help="skip confirmation")
) -> None:
    """Delete an account."""
    from app.db import User, db_session

    if not yes:
        typer.confirm(f"Delete user {username!r}?", abort=True)
    with db_session() as s:
        user = s.scalar(select(User).where(User.username == username))
        if not user:
            raise typer.BadParameter(f"no such user {username!r}")
        s.delete(user)
    typer.echo(f"deleted user {username}")


@searches.command("seed")
def searches_seed(owner: str = typer.Option(..., help="username that owns the search")) -> None:
    """Create the default 'BMW 3 Series E90/E91 318i/320i petrol 2008-2014' search if absent."""
    from app.db import SavedSearch, User, db_session

    with db_session() as s:
        user = s.scalar(select(User).where(User.username == owner))
        if not user:
            raise typer.BadParameter(f"no such user {owner!r}")
        if s.scalar(select(SavedSearch).where(SavedSearch.name == "BMW E90/E91 318i/320i")):
            typer.echo("already exists")
            return
        s.add(
            SavedSearch(
                name="BMW E90/E91 318i/320i",
                owner=user,
                source="autoscout24_de",
                source_params=E90_PARAMS,
                filters={},
            )
        )
    typer.echo("created saved search")


COMPARABLE_SEARCHES: list[tuple[str, str, dict[str, object]]] = [
    (
        "NL comps · AutoScout24.nl E90/E91 318i/320i",
        "autoscout24_nl",
        {
            "path": "/lst/bmw",
            "cat": "ma13gr100038ge25mt102,ma13gr100038ge25mt103",
            "fuel": "B",
            "body": "5,6",
            "fregfrom": 2008,
            "fregto": 2014,
            "cy": "NL",
        },
    ),
    (
        "NL comps · Marktplaats 3-serie 320i",
        "marktplaats",
        {
            "l2CategoryId": 96,
            "model": "3-Serie",
            "fuel": "Benzine",
            "year_from": 2008,
            "year_to": 2014,
            "query": "320i",
            "motor_regex": r"\b320\s?i\b",
            "bodies": ["Sedan", "Stationwagon"],
        },
    ),
    (
        "NL comps · Marktplaats 3-serie 318i",
        "marktplaats",
        {
            "l2CategoryId": 96,
            "model": "3-Serie",
            "fuel": "Benzine",
            "year_from": 2008,
            "year_to": 2014,
            "query": "318i",
            "motor_regex": r"\b318\s?i\b",
            "bodies": ["Sedan", "Stationwagon"],
        },
    ),
    (
        "NL comps · Gaspedaal 3-serie benzine",
        "gaspedaal",
        {
            "path": "/bmw/3-serie/benzine",
            "bmin": 2008,
            "bmax": 2014,
            "motor_regex": r"\b(318i|320i)\b",
            "bodies": ["Sedan", "Stationwagon"],
        },
    ),
]


E90_MOTOR_RE = r"\b(318\s?i|320\s?i)\b"
# Phase 4 deal searches: (name, source, params, interval_minutes)
PHASE4_SEARCHES: list[tuple[str, str, dict[str, object], int]] = [
    (
        "BE · AutoScout24.be E90/E91 318i/320i",
        "autoscout24_be",
        {
            "path": "/nl/lst/bmw",
            "cat": "ma13gr100038ge25mt102,ma13gr100038ge25mt103",
            "fuel": "B",
            "body": "5,6",
            "fregfrom": 2008,
            "fregto": 2014,
            "cy": "B",
        },
        60,
    ),
    (
        "BE · 2dehands 3-serie 320i",
        "tweedehands_be",
        {
            "l2CategoryId": 96,
            "model": "3-Serie",
            "fuel": "Benzine",
            "year_from": 2008,
            "year_to": 2014,
            "query": "320i",
            "motor_regex": r"\b320\s?i\b",
            "bodies": ["Berline", "Break", "Sedan", "Stationwagon"],
        },
        60,
    ),
    (
        "BE · 2dehands 3-serie 318i",
        "tweedehands_be",
        {
            "l2CategoryId": 96,
            "model": "3-Serie",
            "fuel": "Benzine",
            "year_from": 2008,
            "year_to": 2014,
            "query": "318i",
            "motor_regex": r"\b318\s?i\b",
            "bodies": ["Berline", "Break", "Sedan", "Stationwagon"],
        },
        60,
    ),
    (
        "IT · AutoScout24.it Serie 3 318i/320i",
        "autoscout24_it",
        {
            "path": "/lst/bmw/serie-3",
            "fuel": "B",
            "body": "5,6",
            "fregfrom": 2008,
            "fregto": 2014,
            "cy": "I",
            "models": ["318", "320"],
        },
        60,
    ),
    (
        "IT · Automobile.it Serie 3 petrol",
        "automobile_it",
        {
            "path": "/bmw-serie_3",
            "attr_m_year_min": 2008,
            "attr_m_year_max": 2014,
            "fuel": "benzina",
            "motor_regex": E90_MOTOR_RE,
        },
        360,
    ),
    (
        "DE · Kleinanzeigen 320i",
        "kleinanzeigen",
        {
            "make": "bmw",
            "query": "320i",
            "ez_from": 2008,
            "ez_to": 2014,
            "motor_regex": r"\b320\s?i\b",
        },
        60,
    ),
    (
        "DE · Kleinanzeigen 318i",
        "kleinanzeigen",
        {
            "make": "bmw",
            "query": "318i",
            "ez_from": 2008,
            "ez_to": 2014,
            "motor_regex": r"\b318\s?i\b",
        },
        60,
    ),
    (
        "SE · Bytbil 3-serien petrol",
        "bytbil",
        {
            "Makes": "BMW",
            "Models": "3-serien",
            "ModelYearRange.From": 2008,
            "ModelYearRange.To": 2014,
            "Fuels": "Bensin",
            "motor_regex": r"\b(318\s?i|320\s?i)\b",  # Bytbil titles read "320 i"
        },
        360,
    ),
]


@searches.command("seed-phase4")
def searches_seed_phase4(
    owner: str = typer.Option(..., help="username that owns the searches"),
) -> None:
    """Create the phase 4 deal searches (BE, IT, Kleinanzeigen, Bytbil)."""
    from app.db import SavedSearch, User, db_session

    with db_session() as s:
        user = s.scalar(select(User).where(User.username == owner))
        if not user:
            raise typer.BadParameter(f"no such user {owner!r}")
        created = 0
        for name, source, params, interval in PHASE4_SEARCHES:
            if s.scalar(select(SavedSearch).where(SavedSearch.name == name)):
                continue
            s.add(
                SavedSearch(
                    name=name,
                    owner=user,
                    source=source,
                    source_params=params,
                    filters={},
                    role="deals",
                    interval_minutes=interval,
                )
            )
            created += 1
    typer.echo(f"created {created} searches")


insights = typer.Typer(no_args_is_help=True, help="Claude pass over descriptions")
app.add_typer(insights, name="insights")


@insights.command("run")
def insights_run(limit: int = typer.Option(50, help="max listings to analyse now")) -> None:
    """Analyse listings that have a description but no current insight."""
    from app.worker.jobs import analyze_backlog

    counts = analyze_backlog(limit)
    if not counts:
        typer.echo("disabled: ANTHROPIC_API_KEY is not set")
        return
    typer.echo(" ".join(f"{k}={v}" for k, v in counts.items()))


models = typer.Typer(no_args_is_help=True, help="Watchlist (models.yaml) -> searches")
app.add_typer(models, name="models")


@models.command("sync")
def models_sync(
    owner: str = typer.Option(..., help="username that owns created searches"),
    prune: bool = typer.Option(
        False, "--prune", help="pause watchlist searches no longer in models.yaml"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="only print what would be created"),
) -> None:
    """Create the deal and comparables searches for every model in models.yaml."""
    from app.db import User, db_session
    from app.watchlist import build_specs, load_models, sync

    entries = load_models()
    if dry_run:
        for m in entries:
            for sp in build_specs(m):
                typer.echo(
                    f"{sp.role:12} {sp.source:16} every {sp.interval_minutes:>3} min  {sp.name}"
                )
        return
    with db_session() as s:
        user = s.scalar(select(User).where(User.username == owner))
        if not user:
            raise typer.BadParameter(f"no such user {owner!r}")
        r = sync(s, user, entries, prune=prune)
    msg = f"models: {len(entries)}  created {r['created']}  updated {r['updated']}"
    typer.echo(f"{msg}  paused {r['paused']}")
    if r["updated"]:
        typer.echo("(updated includes hand-made searches adopted into the watchlist)")


@models.command("list")
def models_list() -> None:
    """Show the watchlist."""
    from app.watchlist import build_specs, load_models

    for m in load_models():
        y0, y1 = m["years"]
        typer.echo(
            f"{m['key']:<20} {m['label']:<32} {y0}-{y1} {m.get('fuel', 'petrol'):<7} "
            f"{len(build_specs(m))} searches"
        )


alerts = typer.Typer(no_args_is_help=True, help="Telegram alert queue")
app.add_typer(alerts, name="alerts")


@alerts.command("expire")
def alerts_expire(
    hours: int = typer.Option(24, help="expire unsent alerts older than this"),
    all_pending: bool = typer.Option(
        False, "--all", help="expire every unsent alert, regardless of age"
    ),
) -> None:
    """Drop queued alerts so enabling Telegram does not replay a backlog."""
    from datetime import UTC, datetime, timedelta

    from app.alerts.telegram import expire_stale_alerts
    from app.db import db_session

    with db_session() as s:
        n = expire_stale_alerts(s, datetime.now(UTC), timedelta(hours=0 if all_pending else hours))
    typer.echo(f"expired {n} alerts")


@app.command("doctor")
def doctor() -> None:
    """Check configuration, database, Telegram and Claude access. Run this first on a new server."""
    import httpx
    from sqlalchemy import text

    from app.config import get_settings
    from app.db import get_engine

    settings = get_settings()
    ok = True

    def report(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        typer.echo(f"[{'ok' if good else 'FAIL'}] {label}{(': ' + detail) if detail else ''}")

    for p in settings.check_production_secrets():
        report("secrets", False, p)
    if settings.is_production and not settings.check_production_secrets():
        report("secrets", True, "no placeholders")
    report("domain", True, settings.site_url)
    try:
        with get_engine().connect() as conn:
            version = conn.execute(text("select version()")).scalar() or ""
            rev = conn.execute(text("select version_num from alembic_version")).scalar()
        report("database", True, version.split(",")[0])
        report(
            "migrations",
            rev is not None,
            f"revision {rev}" if rev else "not applied: run alembic upgrade head",
        )
    except Exception as e:  # noqa: BLE001
        report("database", False, str(e).splitlines()[0][:160])
    token = settings.telegram_bot_token.get_secret_value()
    if token:
        try:
            r = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
            name = r.json().get("result", {}).get("username")
            report("telegram bot", r.status_code == 200, f"@{name}" if name else r.text[:120])
            if settings.telegram_chat_id:
                r = httpx.get(
                    f"https://api.telegram.org/bot{token}/getChat",
                    params={"chat_id": settings.telegram_chat_id},
                    timeout=10,
                )
                title = r.json().get("result", {}).get("title") or r.json().get("result", {}).get(
                    "username"
                )
                report(
                    "telegram group",
                    r.status_code == 200,
                    title or r.json().get("description", "")[:120],
                )
            else:
                report("telegram group", False, "TELEGRAM_CHAT_ID is empty")
        except httpx.HTTPError as e:
            report("telegram", False, str(e)[:120])
    else:
        report("telegram", True, "disabled (no token)")
    if settings.insights_enabled:
        try:
            import anthropic

            client = anthropic.Anthropic(
                api_key=settings.anthropic_api_key.get_secret_value(), max_retries=0
            )
            client.models.retrieve(settings.claude_model)
            report("claude", True, settings.claude_model)
        except anthropic.AuthenticationError:
            report("claude", False, "API key rejected")
        except anthropic.NotFoundError:
            report("claude", False, f"model {settings.claude_model!r} not found")
        except anthropic.APIError as e:
            report("claude", False, str(e)[:120])
    else:
        report("claude", True, "disabled (no ANTHROPIC_API_KEY)")
    from app.db import SavedSearch, User, db_session

    with db_session() as s:
        users = s.scalar(select(func.count()).select_from(User)) or 0
        searches = s.scalar(select(func.count()).select_from(SavedSearch)) or 0
    report(
        "users",
        users > 0,
        f"{users} (create one with: carscan users create <name>)" if not users else str(users),
    )
    report(
        "saved searches",
        searches > 0,
        f"{searches} (seed with: carscan searches seed --owner <name>)"
        if not searches
        else str(searches),
    )
    typer.echo("all good" if ok else "fix the FAIL lines above")
    raise typer.Exit(code=0 if ok else 1)


def _chunks(ids: list[int], size: int) -> list[list[int]]:
    return [ids[i : i + size] for i in range(0, len(ids), size)]


@app.command("normalize")
def normalize_cmd() -> None:
    """Backfill canonical colour and body type on stored listings and comparables from raw."""
    from sqlalchemy import select

    from app.db import Comparable, Listing, db_session
    from app.normalize.attrs import canonical_body, canonical_color
    from app.worker.jobs import _model_bodies

    def color_from_raw(row: Listing) -> str | None:
        det = (row.raw or {}).get("detail") or {}
        if row.source.startswith("autoscout24"):
            veh = det.get("vehicle") or {}
            return canonical_color(veh.get("bodyColorRaw") or veh.get("bodyColor"))
        if row.source == "kleinanzeigen":
            return canonical_color((det.get("details") or {}).get("Außenfarbe"))
        if row.source == "automobile_it":
            return canonical_color((det.get("info") or {}).get("Colore esterno"))
        return None

    hints = _model_bodies()
    n = c = 0
    with db_session() as s:
        lids = list(s.scalars(select(Listing.id).order_by(Listing.id)))
        cids = list(s.scalars(select(Comparable.id).order_by(Comparable.id)))
    for chunk in _chunks(lids, 300):
        with db_session() as s:
            for row in s.scalars(select(Listing).where(Listing.id.in_(chunk)).order_by(Listing.id)):
                row.color = color_from_raw(row) or row.color
                row.body_type = canonical_body(row.body_type, hints.get(row.model_key or ""))
                n += 1
    for chunk in _chunks(cids, 300):
        with db_session() as s:
            for comp in s.scalars(
                select(Comparable).where(Comparable.id.in_(chunk)).order_by(Comparable.id)
            ):
                comp.body_type = canonical_body(comp.body_type, hints.get(comp.model_key or ""))
                c += 1
    typer.echo(f"normalized {n} listings and {c} comparables")


geo = typer.Typer(help="places and pickup distances")
app.add_typer(geo, name="geo")


@geo.command("run")
def geo_run(limit: int = typer.Option(30, help="max places to geocode now")) -> None:
    """Geocode places of active cars that are not cached yet (the worker does this too)."""
    from app.worker.jobs import geocode_backlog_job

    counts = geocode_backlog_job(limit=limit)
    typer.echo(" ".join(f"{k}={v}" for k, v in counts.items()))


digest = typer.Typer(help="morning digest")
app.add_typer(digest, name="digest")


@digest.command("run")
def digest_run(send: bool = typer.Option(False, "--send", help="also post to Telegram")) -> None:
    """Build today's digest now (shown at /digest); --send posts it to the group."""
    from app.worker.jobs import digest_job

    typer.echo(f"digest #{digest_job(send=send)} built")


@models.command("snapshot")
def models_snapshot() -> None:
    """Take today's per-model history snapshot now (the worker does it nightly)."""
    from app.worker.jobs import snapshot_models_job

    typer.echo(f"snapshot for {snapshot_models_job()} models")


@scrape.command("details")
def scrape_details(
    limit: int = typer.Option(40, help="max ad pages to fetch"),
    listing_id: list[int] | None = typer.Option(  # noqa: B008
        None, "--listing", help="specific listing ids (ignores the spread gate)"
    ),
) -> None:
    """Fetch missing ad pages (description, photos, specs) for stored cars."""
    from app.worker.jobs import enrich_backlog

    counts = enrich_backlog(limit=limit, listing_ids=listing_id or None)
    typer.echo(" ".join(f"{k}={v}" for k, v in counts.items()))


@insights.command("rules")
def insights_rules() -> None:
    """Re-run the keyword rules over every listing that has a description (no API calls)."""
    from datetime import UTC, datetime

    from sqlalchemy import select as _select

    from app.db import Listing, db_session
    from app.insights import apply_rules

    now = datetime.now(UTC)
    n = 0
    with db_session() as s:
        ids = list(
            s.scalars(
                _select(Listing.id).where(Listing.description.isnot(None)).order_by(Listing.id)
            )
        )
    for chunk in _chunks(ids, 200):  # short transactions: the worker keeps running meanwhile
        with db_session() as s:
            for row in s.scalars(
                _select(Listing).where(Listing.id.in_(chunk)).order_by(Listing.id)
            ):
                if apply_rules(row, now) is not None:
                    n += 1
    typer.echo(f"tagged {n} listings")


@app.command("calibrate")
def calibrate_cmd(as_json: bool = typer.Option(False, "--json")) -> None:
    """Compare estimates with logged actuals and print suggested costs.yaml values."""
    import json

    from app.db import db_session
    from app.deals import calibrate

    with db_session() as s:
        report = calibrate(s)
    if as_json:
        typer.echo(json.dumps(report.as_dict(), indent=2, default=str))
        return
    typer.echo(f"{report.deals_bought} bought, {report.deals_sold} sold")
    for m in report.metrics:
        typer.echo(
            f"- {m.name} (n={m.n}): estimated {m.estimated} / actual {m.actual} -> {m.suggestion}"
        )


@dedup.command("run")
def dedup_run() -> None:
    """Re-link duplicates across sources for all active listings."""
    from datetime import UTC, datetime

    from app.db import db_session
    from app.storage import relink_all

    with db_session() as s:
        r = relink_all(s, datetime.now(UTC))
    typer.echo(f"checked {r.checked} listings, linked {r.linked} duplicates")


@searches.command("seed-comparables")
def searches_seed_comparables(
    owner: str = typer.Option(..., help="username that owns the searches"),
    interval: int = typer.Option(360, help="minutes between runs"),
) -> None:
    """Create the default NL comparables searches (AutoScout24.nl, Marktplaats, Gaspedaal)."""
    from app.db import SavedSearch, User, db_session

    with db_session() as s:
        user = s.scalar(select(User).where(User.username == owner))
        if not user:
            raise typer.BadParameter(f"no such user {owner!r}")
        created = 0
        for name, source, params in COMPARABLE_SEARCHES:
            if s.scalar(select(SavedSearch).where(SavedSearch.name == name)):
                continue
            s.add(
                SavedSearch(
                    name=name,
                    owner=user,
                    source=source,
                    source_params=params,
                    filters={},
                    role="comparables",
                    alerts_enabled=False,
                    interval_minutes=interval,
                )
            )
            created += 1
    typer.echo(f"created {created} comparables searches")


@values.command("recompute")
def values_recompute() -> None:
    """Recompute NL value estimates and spreads for active listings."""
    from datetime import UTC, datetime

    from app.db import db_session
    from app.valuation import recompute_values

    with db_session() as s:
        n = recompute_values(s, datetime.now(UTC), batch_commit=500)
    typer.echo(f"recomputed {n} listings")


@searches.command("list")
def searches_list() -> None:
    """List saved searches."""
    from app.db import SavedSearch, db_session

    with db_session() as s:
        for x in s.scalars(select(SavedSearch).order_by(SavedSearch.id)):
            flags = ("active" if x.active else "paused") + (", alerts" if x.alerts_enabled else "")
            typer.echo(
                f"{x.id:>3}  {x.name:<44} {x.source:<16} {x.role:<12} "
                f"every {x.interval_minutes} min, {flags}"
            )


@scrape.command("run")
def scrape_run(
    search_id: int | None = typer.Option(None, help="only this saved search"),
    detail_budget: int = typer.Option(40, help="max detail pages to fetch this run"),
    due_only: bool = typer.Option(
        False, "--due-only", help="skip searches whose interval has not elapsed"
    ),
) -> None:
    """Run the scrapers now (all active searches, or one)."""
    from app.config import get_settings
    from app.logging import configure_logging
    from app.worker.jobs import run_all_active_searches, run_saved_search

    configure_logging(get_settings().log_level)
    summaries = (
        [run_saved_search(search_id, detail_budget=detail_budget)]
        if search_id
        else run_all_active_searches(detail_budget=detail_budget, due_only=due_only)
    )
    for r in summaries:
        typer.echo(
            f"run {r.run_id}: {r.status.value} seen={r.seen} new={r.new} "
            f"drops={r.price_dropped} deactivated={r.deactivated} alerts={r.alerts_queued}"
            + (f" error={r.error}" if r.error else "")
        )


@costs.command("recompute")
def costs_recompute(
    only_stale: bool = typer.Option(
        False, "--only-stale", help="only listings computed with an older costs.yaml"
    ),
) -> None:
    """Recompute landed costs for stored listings (run after editing costs.yaml)."""
    from datetime import UTC, datetime

    from app.db import db_session
    from app.storage import recompute_costs

    with db_session() as s:
        n = recompute_costs(s, datetime.now(UTC), only_stale=only_stale, batch_commit=500)
    typer.echo(f"recomputed {n} listings")


@costs.command("show")
def costs_show(
    price: int,
    first_registration: str = typer.Option(..., help="YYYY-MM-DD"),
    co2: int | None = typer.Option(None, help="NEDC g/km; omitted = model fallback"),
    make: str = "BMW",
    model: str = "320i",
    country: str = "DE",
) -> None:
    """Print the landed-cost breakdown for a hypothetical car."""
    import json
    from datetime import date

    from app.costs import landed_cost

    lc = landed_cost(
        price_eur=price,
        country=country,
        first_registration=date.fromisoformat(first_registration),
        co2_g_km=co2,
        fuel="petrol",
        make=make,
        model=model,
    )
    typer.echo(json.dumps(lc.as_dict(), indent=2))


if __name__ == "__main__":
    app()
