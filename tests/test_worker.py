from pathlib import Path

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from app.cli import app as cli
from app.db import Alert, Listing, SavedSearch, ScrapeRun, ScrapeStatus, User
from app.scrapers import PoliteClient
from app.worker.jobs import run_saved_search

FIX = Path(__file__).parent / "fixtures" / "autoscout24_de"


def _transport(pages: dict[str, str], detail_status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/angebote/"):
            if detail_status != 200:
                return httpx.Response(detail_status)
            return httpx.Response(200, text=(FIX / "detail_dealer.html").read_text())
        page = request.url.params.get("page", "1")
        return httpx.Response(200, text=(FIX / pages.get(page, "search_empty.html")).read_text())

    return httpx.MockTransport(handler)


def _client(pages: dict[str, str], **kw: int) -> PoliteClient:
    return PoliteClient(transport=_transport(pages, **kw), sleep=lambda _: None)


def _seed(db: Session) -> int:
    u = User(username="u", password_hash="x")
    s = SavedSearch(
        name="e90", owner=u, source="autoscout24_de", source_params={"path": "/lst/bmw/3er"}
    )
    db.add_all([u, s])
    db.commit()
    return s.id


def test_first_run_then_shrinking_run(db: Session) -> None:
    sid = _seed(db)
    r1 = run_saved_search(
        sid, client=_client({"1": "search_page1.html", "2": "search_page2.html"}), detail_budget=3
    )
    assert r1.status is ScrapeStatus.ok and r1.new > 20 and r1.deactivated == 0
    assert r1.alerts_queued == r1.new
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Listing)) == r1.new
    enriched = db.scalar(
        select(func.count()).select_from(Listing).where(Listing.raw["detail"].isnot(None))
    )
    assert enriched == 3  # detail budget respected
    run = db.get(ScrapeRun, r1.run_id)
    assert run is not None and run.status is ScrapeStatus.ok and run.listings_new == r1.new

    # second run only returns page 1: page-2-only listings get deactivated, nothing new, no alerts
    r2 = run_saved_search(sid, client=_client({"1": "search_page1.html"}), detail_budget=0)
    assert (
        r2.status is ScrapeStatus.ok
        and r2.new == 0
        and r2.deactivated > 0
        and r2.alerts_queued == 0
    )
    db.expire_all()
    inactive = db.scalar(
        select(func.count()).select_from(Listing).where(Listing.is_active.is_(False))
    )
    assert inactive == r2.deactivated
    assert db.scalar(select(func.count()).select_from(Alert)) == r1.new


def test_blocked_source_records_failed_run(db: Session) -> None:
    sid = _seed(db)
    blocked = PoliteClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(403)),
        sleep=lambda _: None,
        retries=0,
    )
    r = run_saved_search(sid, client=blocked)
    assert r.status is ScrapeStatus.failed and r.error and "BlockedError" in r.error
    run = db.get(ScrapeRun, r.run_id)
    assert run is not None and run.status is ScrapeStatus.failed and "403" in (run.error or "")


def test_parser_break_records_failed_run(db: Session) -> None:
    sid = _seed(db)
    weird = PoliteClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, text="<html>new layout</html>")
        ),
        sleep=lambda _: None,
    )
    r = run_saved_search(sid, client=weird)
    assert r.status is ScrapeStatus.failed and "ParseError" in (r.error or "")


def test_cli_seed_and_list(db: Session) -> None:
    db.add(User(username="q", password_hash="x"))
    db.commit()
    r = CliRunner().invoke(cli, ["searches", "seed", "--owner", "q"])
    assert r.exit_code == 0 and "created" in r.output, r.output
    r = CliRunner().invoke(cli, ["searches", "seed", "--owner", "q"])
    assert "already exists" in r.output
    r = CliRunner().invoke(cli, ["searches", "list"])
    assert "BMW E90/E91 318i/320i" in r.output and "autoscout24_de" in r.output
