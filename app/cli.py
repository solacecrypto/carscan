"""Admin CLI. Inside the container: `docker compose exec web carscan --help`."""

import typer
from sqlalchemy import select

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
            "motor_regex": r"\b320i\b",
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
            "motor_regex": r"\b318i\b",
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


E90_MOTOR_RE = r"\b(318i|320i)\b"
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
            "motor_regex": r"\b320i\b",
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
            "motor_regex": r"\b318i\b",
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
            "motor_regex": r"\b320i\b",
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
            "motor_regex": r"\b318i\b",
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
            "motor_regex": r"\b(318|320)\s?i\b",  # Bytbil titles read "320 i"
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
        n = recompute_values(s, datetime.now(UTC))
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
        n = recompute_costs(s, datetime.now(UTC), only_stale=only_stale)
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
