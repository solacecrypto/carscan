"""The scrape job: one saved search, one source, one run."""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alerts import notify_admin, queue_alerts
from app.costs import landed_cost
from app.db import ScrapeRun, ScrapeStatus, db_session
from app.db.models import Listing, SavedSearch
from app.insights import analyze_pending, default_analyzer
from app.normalize import NormalizedListing, RawListing
from app.scrapers import BlockedError, ParseError, PoliteClient, ScrapeError, get_scraper
from app.storage import (
    deactivate_missing,
    deactivate_missing_comparables,
    link_duplicates,
    upsert_comparables,
    upsert_listings,
)
from app.valuation import load_comps, recompute_values
from app.valuation.engine import Target, ValuationConfig, estimate_value

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
    insights_budget: int | None = None,
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

    from app.config import get_settings

    settings = get_settings()
    if insights_budget is None:
        insights_budget = settings.insights_per_run
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
                items = _apply_watchlist(params, [scraper.parse(r) for r in raws])
                cres = upsert_comparables(s, items, search, now)
                gone = deactivate_missing_comparables(s, search, run_started_at=started)
                summary = RunSummary(
                    run_id, ScrapeStatus.ok, seen=cres.seen, new=cres.new, deactivated=gone
                )
                price_changed = 0
            else:
                items = _apply_watchlist(params, [scraper.parse(r) for r in raws])
                chosen = _select_for_enrichment(s, raws, items, settings.detail_min_spread_eur)
                _enrich_new(s, scraper, client, chosen, detail_budget)
                items = _apply_watchlist(params, [scraper.parse(r) for r in raws])
                result = upsert_listings(s, items, search, now)
                stale = deactivate_missing(s, search, run_started_at=started)
                dedup = link_duplicates(s, result.new, now)
                if dedup.linked:
                    log.info("scrape.dedup", linked=dedup.linked, checked=dedup.checked)
                recompute_values(s, now)
                analyzer = default_analyzer() if insights_budget else None
                if analyzer and result.new:
                    floor = settings.insights_min_spread_eur
                    ids = [
                        x.id
                        for x in result.new
                        if floor is None or x.spread_eur is None or x.spread_eur >= floor
                    ]
                    counts = analyze_pending(s, analyzer, now, insights_budget, ids)
                    log.info("scrape.insights", candidates=len(ids), **counts)
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


def _apply_watchlist(
    params: dict[str, Any], items: list[NormalizedListing]
) -> list[NormalizedListing]:
    """Stamp the watchlist's canonical make and model key on every parsed listing."""
    make, key = params.get("make_label"), params.get("model_key")
    for it in items:
        if make:
            it.make = make
        if key:
            it.model_key = key
    return items


def _select_for_enrichment(
    s: Session, raws: list[RawListing], items: list[NormalizedListing], min_spread: int | None
) -> list[RawListing]:
    """Detail pages only for cars whose provisional spread (search data + current comparables)
    clears the floor. Cars the valuation cannot price yet always qualify."""
    if min_spread is None:
        return raws
    from app.costs import get_cost_config

    comps = load_comps(s)
    vcfg = ValuationConfig.from_mapping(get_cost_config().valuation)
    chosen: list[RawListing] = []
    for raw, item in zip(raws, items, strict=True):
        lc = landed_cost(
            price_eur=item.price_eur,
            country=item.country,
            first_registration=item.first_registration,
            co2_g_km=item.co2_g_km,
            fuel=item.fuel,
            make=item.make,
            model=item.model,
        )
        year = item.first_registration.year if item.first_registration else None
        est = estimate_value(
            Target(item.model_key or item.model, item.body_type, year, item.mileage_km), comps, vcfg
        )
        if lc.total_eur is None or est is None or est.value_eur - lc.total_eur >= min_spread:
            chosen.append(raw)
    if len(chosen) < len(raws):
        log.info(
            "scrape.enrich.gated", eligible=len(chosen), total=len(raws), min_spread=min_spread
        )
    return chosen


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
    """Active searches whose interval has elapsed, longest-waiting first. Never-run searches go
    before all others, comparables first so a new model has NL prices before its cars arrive."""
    candidates: list[tuple[datetime | None, bool, int]] = []
    for search in session.scalars(select(SavedSearch).where(SavedSearch.active.is_(True))):
        last = session.scalar(
            select(ScrapeRun.started_at)
            .where(ScrapeRun.saved_search_id == search.id)
            .order_by(ScrapeRun.started_at.desc())
            .limit(1)
        )
        if (
            due_only
            and last is not None
            and last > now - timedelta(minutes=search.interval_minutes)
        ):
            continue
        candidates.append((last, search.role == "comparables", search.id))
    candidates.sort(key=lambda t: (t[0] is not None, t[0] is None and not t[1], t[0] or now, t[2]))
    return [i for _, _, i in candidates]


def cleanup_interrupted_runs(session: Session, now: datetime, max_age_minutes: int = 120) -> int:
    """Runs still 'running' after a worker restart can never finish: mark them failed."""
    stale = list(
        session.scalars(
            select(ScrapeRun).where(
                ScrapeRun.status == ScrapeStatus.running,
                ScrapeRun.started_at < now - timedelta(minutes=max_age_minutes),
            )
        )
    )
    for r in stale:
        r.status = ScrapeStatus.failed
        r.finished_at = now
        r.error = "interrupted (worker restarted)"
    return len(stale)


def run_all_active_searches(
    detail_budget: int = DETAIL_FETCH_BUDGET, due_only: bool = False
) -> list[RunSummary]:
    with db_session() as s:
        ids = due_search_ids(s, datetime.now(UTC), due_only)
    return [run_saved_search(i, detail_budget=detail_budget) for i in ids]


def run_due_searches() -> list[RunSummary]:
    with db_session() as s:
        n = cleanup_interrupted_runs(s, datetime.now(UTC), max_age_minutes=0)
    if n:
        log.warning("scrape.interrupted_runs_closed", count=n)
    return run_all_active_searches(due_only=True)


def enrich_backlog(
    limit: int = DETAIL_FETCH_BUDGET,
    listing_ids: list[int] | None = None,
    client: PoliteClient | None = None,
) -> dict[str, int]:
    """Fetch the ad page for stored cars that never got one (added before a fetcher existed,
    or outside the per-run budget). Same spread gate as new cars unless ids are given."""
    from sqlalchemy.orm import selectinload

    from app.config import get_settings

    settings = get_settings()
    floor = settings.detail_min_spread_eur
    counts = {"fetched": 0, "gone": 0, "skipped": 0}
    with db_session() as s:
        stmt = (
            select(Listing)
            .options(selectinload(Listing.saved_searches))
            .where(Listing.is_active.is_(True), Listing.duplicate_of_id.is_(None))
            .where((Listing.description.is_(None)) | (Listing.description == ""))
            .where(~Listing.raw.has_key("detail"))
            .order_by(Listing.spread_eur.desc().nulls_first(), Listing.first_seen.desc())
        )
        if listing_ids:
            stmt = stmt.where(Listing.id.in_(listing_ids))
        elif floor is not None:
            stmt = stmt.where((Listing.spread_eur.is_(None)) | (Listing.spread_eur >= floor))
        rows = list(s.scalars(stmt.limit(limit)))
        if not rows:
            return counts
        own = client is None
        client = client or PoliteClient()
        now = datetime.now(UTC)
        try:
            for row in rows:
                scraper = get_scraper(row.source)
                search_data = (row.raw or {}).get("search")
                if not search_data or not row.saved_searches:
                    counts["skipped"] += 1
                    continue
                raw = RawListing(
                    source=row.source,
                    source_listing_id=row.source_listing_id,
                    url=row.url,
                    data=search_data,
                    fetched_at=now,
                )
                try:
                    detail = scraper.fetch_detail(raw, client)
                except ParseError as e:
                    log.warning("enrich.backlog.failed", listing=row.id, error=str(e))
                    counts["skipped"] += 1
                    continue
                if detail is None:
                    row.is_active = False
                    counts["gone"] += 1
                    continue
                raw.detail = detail
                search = row.saved_searches[0]
                items = _apply_watchlist(dict(search.source_params), [scraper.parse(raw)])
                upsert_listings(s, items, search, now)
                counts["fetched"] += 1
        finally:
            if own:
                client.close()
        recompute_values(s, now)
    if any(counts.values()):
        log.info("enrich.backlog", **counts)
    return counts


def analyze_backlog(limit: int | None = None) -> dict[str, int]:
    """Catch-up pass: listings that still lack a current insight (older ones, budget overflow)."""
    from app.config import get_settings

    analyzer = default_analyzer()
    if analyzer is None:
        return {}
    with db_session() as s:
        counts = analyze_pending(
            s, analyzer, datetime.now(UTC), limit or get_settings().insights_per_run
        )
    if any(counts.values()):
        log.info("insights.backlog", **counts)
    return counts
