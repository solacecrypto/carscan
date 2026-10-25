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
from app.alerts.watch import check_watches
from app.costs import landed_cost
from app.db import ScrapeRun, ScrapeStatus, db_session
from app.db.models import Digest, Listing, SavedSearch
from app.insights import analyze_pending, default_analyzer
from app.normalize import NormalizedListing, RawListing
from app.normalize.attrs import canonical_body
from app.notify import favorite_events, spread_events, starred_spreads, subscription_events
from app.scrapers import BlockedError, ParseError, PoliteClient, ScrapeError, get_scraper
from app.storage import (
    deactivate_missing,
    deactivate_missing_comparables,
    link_duplicates,
    upsert_comparables,
    upsert_listings,
)
from app.valuation import load_comps, recompute_values
from app.valuation.engine import Target, ValuationConfig, engine_token, estimate_value

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
                items = _apply_watchlist(params, _parse_all(scraper, raws))
                cres = upsert_comparables(s, items, search, now)
                gone = deactivate_missing_comparables(s, search, run_started_at=started)
                summary = RunSummary(
                    run_id, ScrapeStatus.ok, seen=cres.seen, new=cres.new, deactivated=gone
                )
                price_changed = 0
            else:
                items = _apply_watchlist(params, _parse_all(scraper, raws))
                chosen = _select_for_enrichment(s, raws, items, settings.detail_min_spread_eur)
                _enrich_new(s, scraper, client, chosen, detail_budget)
                items = _apply_watchlist(params, _parse_all(scraper, raws))
                result = upsert_listings(s, items, search, now)
                stale = deactivate_missing(s, search, run_started_at=started)
                dedup = link_duplicates(s, result.new, now)
                if dedup.linked:
                    log.info("scrape.dedup", linked=dedup.linked, checked=dedup.checked)
                spreads_before = starred_spreads(s, params.get("model_key"))
                recompute_values(s, now, model_key=params.get("model_key"))
                spread_events(s, spreads_before, now)
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
                alerts += check_watches(s, result, stale, now)
                notified = favorite_events(s, result, stale, now)
                notified += subscription_events(s, result.new, now)
                if notified:
                    log.info("notify.events", count=notified)
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
            if role == "comparables":
                # new NL prices only move the values of this model's cars
                spreads_before = starred_spreads(s, params.get("model_key"))
                recompute_values(s, now, model_key=params.get("model_key"))
                spread_events(s, spreads_before, now)
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
        if isinstance(e, BlockedError):
            # a blocked site stays blocked for a while: stop knocking, it only prolongs the ban
            _SOURCE_COOLDOWN[source] = datetime.now(UTC) + timedelta(
                minutes=SOURCE_COOLDOWN_MINUTES
            )
            log.warning("scrape.source_cooldown", source=source, minutes=SOURCE_COOLDOWN_MINUTES)
        return RunSummary(run_id, ScrapeStatus.failed, error=err)
    finally:
        if own_client:
            client.close()


def _parse_all(scraper: Any, raws: list[RawListing]) -> list[NormalizedListing]:
    """Parse every ad; one unparseable ad (no price, odd markup) is skipped, not fatal.
    The run still fails when nothing at all parses, which means the site changed."""
    items: list[NormalizedListing] = []
    skipped: list[str] = []
    for raw in raws:
        try:
            items.append(scraper.parse(raw))
        except ParseError as e:
            skipped.append(f"{raw.source_listing_id}: {e}")
    if skipped:
        log.warning(
            "scrape.items_skipped", source=scraper.name, count=len(skipped), first=skipped[0]
        )
    if raws and not items:
        raise ParseError(f"none of {len(raws)} ads parsed; first: {skipped[0]}")
    return items


def _apply_watchlist(
    params: dict[str, Any], items: list[NormalizedListing]
) -> list[NormalizedListing]:
    """Stamp the watchlist's canonical make and model key on every parsed listing."""
    make, key = params.get("make_label"), params.get("model_key")
    for it in items:
        if make:
            it.make = make
        if key:
            it.model_key = _generation_for(key, it)
        it.body_type = canonical_body(it.body_type, _model_bodies().get(it.model_key or ""))
    return items


def _generation_for(key: str, it: NormalizedListing) -> str | None:
    """Sites without a generation filter (Kleinanzeigen, Automobile.it) return every 3er or
    Golf: pick the watchlist model whose years cover the registration, preferring the
    search's own model, then a sibling of the same make with a shared motor."""
    year = it.first_registration.year if it.first_registration else None
    models = _models_by_key()
    own = models.get(key)
    if year is None or own is None:
        return key
    lo, hi = own["years"]
    if lo <= year <= hi:
        return key
    motor = engine_token(it.variant, it.model)
    for m in models.values():
        if m["key"] == key or m.get("make") != own.get("make"):
            continue
        ylo, yhi = m["years"]
        shared = motor and any(engine_token(x) == motor for x in m.get("motors") or [])
        if ylo <= year <= yhi and shared:
            return m["key"]
    return None  # a generation we do not track: valued by model name only


_MODELS_CACHE: dict[str, dict[str, Any]] | None = None


def _models_by_key() -> dict[str, dict[str, Any]]:
    global _MODELS_CACHE
    if _MODELS_CACHE is None:
        from app.watchlist import load_models

        try:
            _MODELS_CACHE = {m["key"]: m for m in load_models()}
        except FileNotFoundError:
            _MODELS_CACHE = {}
    return _MODELS_CACHE


_BODIES_CACHE: dict[str, list[str]] | None = None


def _model_bodies() -> dict[str, list[str]]:
    """Watchlist model key -> bodies it covers (a hint for generic site labels)."""
    global _BODIES_CACHE
    if _BODIES_CACHE is None:
        from app.watchlist import load_models

        try:
            _BODIES_CACHE = {m["key"]: list(m.get("bodies") or []) for m in load_models()}
        except FileNotFoundError:
            _BODIES_CACHE = {}
    return _BODIES_CACHE


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


def _interval_factor() -> float:
    from app.config import get_settings

    return max(0.1, float(get_settings().scrape_interval_factor))


def dormant() -> bool:
    from app.config import get_settings

    return bool(get_settings().dormant)


DORMANT_COMPARABLES_MINUTES = 7 * 24 * 60


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
        minutes = search.interval_minutes * _interval_factor()
        if dormant():
            if search.role != "comparables":
                continue  # no foreign scraping while dormant
            minutes = max(minutes, DORMANT_COMPARABLES_MINUTES)
        interval = timedelta(minutes=minutes)
        if due_only and last is not None and last > now - interval:
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


SOURCE_COOLDOWN_MINUTES = 120
_SOURCE_COOLDOWN: dict[str, datetime] = {}


def source_cooling(source: str, now: datetime) -> bool:
    until = _SOURCE_COOLDOWN.get(source)
    if until is None:
        return False
    if now >= until:
        del _SOURCE_COOLDOWN[source]
        return False
    return True


def run_all_active_searches(
    detail_budget: int = DETAIL_FETCH_BUDGET, due_only: bool = False
) -> list[RunSummary]:
    now = datetime.now(UTC)
    with db_session() as s:
        ids = due_search_ids(s, now, due_only)
        rows = s.execute(
            select(SavedSearch.id, SavedSearch.source).where(SavedSearch.id.in_(ids))
        ).all()
        sources = {sid: src for sid, src in rows}
    todo = [i for i in ids if not source_cooling(sources.get(i, ""), now)]
    if len(todo) < len(ids):
        log.info("scrape.skipped_cooling", count=len(ids) - len(todo))
    from app.health import beat

    beat("scrape_due", True, detail={"due": len(todo)})  # alive, even when nothing is due
    out = []
    for i in todo:
        out.append(run_saved_search(i, detail_budget=detail_budget))
        beat("scrape_due", True)  # a pass can take an hour: beat per search, not per pass
    return out


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
    if dormant():
        return counts
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
                s.commit()  # release row locks between slow page fetches
        finally:
            if own:
                client.close()
        recompute_values(s, now)
    if any(counts.values()):
        log.info("enrich.backlog", **counts)
    return counts


def translate_backlog(limit: int | None = None) -> dict[str, int]:
    """Catch-up pass: English text for descriptions that have none yet (TRANSLATE_AUTO=true;
    by default translation happens on demand from the car page)."""
    from app.config import get_settings
    from app.translate import default_translator, translate_pending

    if dormant() or not get_settings().translate_auto:
        return {}

    global _TRANSLATE_COOLDOWN_UNTIL
    translator = default_translator()
    if translator is None:
        return {}
    now = datetime.now(UTC)
    if now < _TRANSLATE_COOLDOWN_UNTIL:
        return {}
    with db_session() as s:
        counts = translate_pending(s, translator, now, limit or get_settings().translate_per_run)
    if counts.get("failed") and not counts.get("translated"):
        # the provider turned us away (302 to a captcha, 429): give it a rest instead of
        # knocking every five minutes, which only prolongs the block
        _TRANSLATE_COOLDOWN_UNTIL = now + timedelta(minutes=TRANSLATE_COOLDOWN_MINUTES)
        log.warning("translate.cooldown", minutes=TRANSLATE_COOLDOWN_MINUTES)
    if any(counts.values()):
        log.info("translate.backlog", **counts)
    return counts


TRANSLATE_COOLDOWN_MINUTES = 20
_TRANSLATE_COOLDOWN_UNTIL = datetime.min.replace(tzinfo=UTC)


def geocode_backlog_job(limit: int = 30) -> dict[str, int]:
    """Resolve the places of active cars through Nominatim, a few per run, cached forever."""
    if dormant():
        return {}
    from app.config import get_settings
    from app.geo import geocode_backlog

    st = get_settings()
    with db_session() as s:
        counts = geocode_backlog(
            s, limit=limit, base=(st.base_city, st.base_postcode, st.base_country)
        )
    if any(counts.values()):
        log.info("geo.backlog", **counts)
    return counts


def digest_job(send: bool = True) -> int:
    """Morning digest: build, store, and post to the group when Telegram is configured."""
    if dormant():
        return 0
    from app.alerts import notify_group
    from app.config import get_settings
    from app.digest import store_digest

    settings = get_settings()
    now = datetime.now(UTC)
    with db_session() as s:
        row = store_digest(s, now, settings.site_url)
        text, digest_id = row.text, row.id
    if send and settings.telegram_enabled:
        notify_group(text)
        with db_session() as s:
            d = s.get(Digest, digest_id)
            if d is not None:
                d.sent_at = datetime.now(UTC)
    log.info("digest.built", day=now.date().isoformat(), sent=send and settings.telegram_enabled)
    return digest_id


_KNOWN_ISSUES: set[str] = set()


def health_job() -> list[str]:
    """Every 5 min: evaluate the health rules; a newly appeared issue goes to every user as a
    notification and to the admin chat. Resolved issues are logged."""
    from app.db.models import User
    from app.health import check
    from app.notify import push

    now = datetime.now(UTC)
    with db_session() as s:
        issues = check(s, now)
        keys = {i.key for i in issues}
        new = [i for i in issues if i.key not in _KNOWN_ISSUES]
        if new:
            users = list(s.scalars(select(User.id)))
            for i in new:
                log.error("health.issue", key=i.key, title=i.title, detail=i.detail)
                for uid in users:
                    push(s, uid, "system", f"⚠ {i.title}", i.detail, now, url="/status")
                notify_admin(f"[health] {i.title}: {i.detail}")
        for key in _KNOWN_ISSUES - keys:
            log.info("health.resolved", key=key)
        _KNOWN_ISSUES.clear()
        _KNOWN_ISSUES.update(keys)
    return sorted(keys)


def prune_job() -> dict[str, int]:
    """Weekly: trim raw data of long-gone cars, drop old runs and notifications."""
    from app.maintenance import prune

    with db_session() as s:
        return prune(s, datetime.now(UTC))


def backup_job() -> dict[str, str | int]:
    """Nightly pg_dump to BACKUP_DIR (and the bucket when configured)."""
    from app import backup

    info = backup.nightly()
    log.info("backup.nightly", **info)
    return info


def snapshot_models_job() -> int:
    """Nightly: one history row per watchlist model (supply, valued share, median spread)."""
    from app.market.history import snapshot_models

    with db_session() as s:
        n = snapshot_models(s, datetime.now(UTC).date())
    log.info("models.snapshot", models=n)
    return n


def analyze_backlog(limit: int | None = None) -> dict[str, int]:
    """Catch-up pass: listings that still lack a current insight (older ones, budget overflow)."""
    if dormant():
        return {}
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
