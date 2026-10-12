"""The scrape job: one saved search, one source, one run."""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alerts import notify_admin, queue_alerts
from app.db import ScrapeRun, ScrapeStatus, db_session
from app.db.models import Listing, SavedSearch
from app.normalize import RawListing
from app.scrapers import BlockedError, ParseError, PoliteClient, ScrapeError, get_scraper
from app.storage import (
    deactivate_missing,
    deactivate_missing_comparables,
    upsert_comparables,
    upsert_listings,
)
from app.valuation import recompute_values

log = structlog.get_logger()

DETAIL_FETCH_BUDGET = 40  # detail pages per run; the rest are enriched on later runs


@dataclass
class RunSummary:
    run_id: int
    status: ScrapeStatus
    seen: int = 0
    new: int = 0
    price_dropped: int = 0
    deactivated: int = 0
    alerts_queued: int = 0
    error: str | None = None


def run_saved_search(
    saved_search_id: int,
    *,
    client: PoliteClient | None = None,
    detail_budget: int = DETAIL_FETCH_BUDGET,
) -> RunSummary:
    started = datetime.now(UTC)
    with db_session() as s:
        search = s.get(SavedSearch, saved_search_id)
        if search is None:
            raise ValueError(f"saved search {saved_search_id} not found")
        run = ScrapeRun(
            source=search.source,
            saved_search_id=search.id,
            started_at=started,
            status=ScrapeStatus.running,
        )
        s.add(run)
        s.flush()
        run_id = run.id
        source, params, name, role = (
            search.source,
            dict(search.source_params),
            search.name,
            search.role,
        )

    log.info("scrape.start", run_id=run_id, source=source, search=name)
    scraper = get_scraper(source)
    own_client = client is None
    client = client or PoliteClient()
    try:
        raws = scraper.search(params, client)
        with db_session() as s:
            search = s.get(SavedSearch, saved_search_id)
            assert search is not None
            now = datetime.now(UTC)
            if role == "comparables":
                items = [scraper.parse(r) for r in raws]
                cres = upsert_comparables(s, items, search, now)
                gone = deactivate_missing_comparables(s, search, run_started_at=started)
                summary = RunSummary(
                    run_id, ScrapeStatus.ok, seen=cres.seen, new=cres.new, deactivated=gone
                )
                price_changed = 0
            else:
                _enrich_new(s, scraper, client, raws, detail_budget)
                items = [scraper.parse(r) for r in raws]
                result = upsert_listings(s, items, search, now)
                stale = deactivate_missing(s, search, run_started_at=started)
                alerts = queue_alerts(s, search, result, now)
                summary = RunSummary(
                    run_id,
                    ScrapeStatus.ok,
                    seen=result.seen,
                    new=len(result.new),
                    price_dropped=len(result.price_dropped),
                    deactivated=len(stale),
                    alerts_queued=len(alerts),
                )
                price_changed = len(result.price_dropped) + len(result.price_raised)
            recompute_values(s, now)
            run = s.get(ScrapeRun, run_id)
            assert run is not None
            run.finished_at = now
            run.status = ScrapeStatus.ok
            run.pages_fetched = client.requests_made
            run.listings_seen = summary.seen
            run.listings_new = summary.new
            run.listings_price_changed = price_changed
            run.listings_deactivated = summary.deactivated
        log.info("scrape.done", **summary.__dict__)
        return summary
    except (ScrapeError, Exception) as e:  # noqa: BLE001 - every failure must be recorded
        err = f"{type(e).__name__}: {e}"
        log.error("scrape.failed", run_id=run_id, error=err, tb=traceback.format_exc())
        with db_session() as s:
            run = s.get(ScrapeRun, run_id)
            assert run is not None
            run.finished_at = datetime.now(UTC)
            run.status = ScrapeStatus.failed
            run.pages_fetched = client.requests_made
            run.error = err
        kind = (
            "BLOCKED"
            if isinstance(e, BlockedError)
            else "PARSER BROKEN"
            if isinstance(e, ParseError)
            else "FAILED"
        )
        notify_admin(f"[{source}] {kind} while running '{name}': {err}")
        return RunSummary(run_id, ScrapeStatus.failed, error=err)
    finally:
        if own_client:
            client.close()


def _enrich_new(
    s: Session, scraper, client: PoliteClient, raws: list[RawListing], budget: int
) -> None:
    """Fetch detail pages for listings we don't have details for yet (bounded per run)."""
    if budget <= 0 or not raws:
        return
    ids = [r.source_listing_id for r in raws]
    have_detail = set(
        s.scalars(
            select(Listing.source_listing_id).where(
                Listing.source == raws[0].source,
                Listing.source_listing_id.in_(ids),
                Listing.raw["detail"].isnot(None),
            )
        )
    )
    fetched = 0
    for raw in raws:
        if raw.source_listing_id in have_detail:
            continue
        if fetched >= budget:
            log.info("scrape.enrich.budget_exhausted", remaining=len(raws) - fetched)
            break
        try:
            raw.detail = scraper.fetch_detail(raw, client)
        except ParseError as e:
            log.warning("scrape.enrich.failed", listing=raw.source_listing_id, error=str(e))
        fetched += 1


def due_search_ids(session: Session, now: datetime, due_only: bool) -> list[int]:
    """Active searches, optionally only those whose interval has elapsed since their last run."""
    ids: list[int] = []
    for search in session.scalars(select(SavedSearch).where(SavedSearch.active.is_(True))):
        if due_only:
            last = session.scalar(
                select(ScrapeRun.started_at)
                .where(ScrapeRun.saved_search_id == search.id)
                .order_by(ScrapeRun.started_at.desc())
                .limit(1)
            )
            if last is not None and last > now - timedelta(minutes=search.interval_minutes):
                continue
        ids.append(search.id)
    return ids


def run_all_active_searches(
    detail_budget: int = DETAIL_FETCH_BUDGET, due_only: bool = False
) -> list[RunSummary]:
    with db_session() as s:
        ids = due_search_ids(s, datetime.now(UTC), due_only)
    return [run_saved_search(i, detail_budget=detail_budget) for i in ids]


def run_due_searches() -> list[RunSummary]:
    return run_all_active_searches(due_only=True)
