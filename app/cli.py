"""Admin CLI. Inside the container: `docker compose exec web carscan --help`."""

import typer
from sqlalchemy import select

app = typer.Typer(no_args_is_help=True, help="Car Scanner admin commands")
users = typer.Typer(no_args_is_help=True, help="Manage login accounts")
searches = typer.Typer(no_args_is_help=True, help="Manage saved searches")
scrape = typer.Typer(no_args_is_help=True, help="Run scrapers")
costs = typer.Typer(no_args_is_help=True, help="Landed-cost estimates")
app.add_typer(costs, name="costs")
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


@searches.command("list")
def searches_list() -> None:
    """List saved searches."""
    from app.db import SavedSearch, db_session

    with db_session() as s:
        for x in s.scalars(select(SavedSearch).order_by(SavedSearch.id)):
            flags = ("active" if x.active else "paused") + (", alerts" if x.alerts_enabled else "")
            typer.echo(f"{x.id:>3}  {x.name:<32} {x.source:<16} {flags}")


@scrape.command("run")
def scrape_run(
    search_id: int | None = typer.Option(None, help="only this saved search"),
    detail_budget: int = typer.Option(40, help="max detail pages to fetch this run"),
) -> None:
    """Run the scrapers now (all active searches, or one)."""
    from app.config import get_settings
    from app.logging import configure_logging
    from app.worker.jobs import run_all_active_searches, run_saved_search

    configure_logging(get_settings().log_level)
    summaries = (
        [run_saved_search(search_id, detail_budget=detail_budget)]
        if search_id
        else run_all_active_searches(detail_budget=detail_budget)
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
