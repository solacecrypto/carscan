"""Keep the database small: cars that are long gone keep their facts and price history but
lose the site's raw JSON and extra photos; old scrape runs and notifications go away."""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import Comparable, Listing, Notification, ScrapeRun

log = structlog.get_logger()
INACTIVE_DAYS = 7
RUNS_DAYS = 90
NOTIFICATIONS_DAYS = 30


def prune(session: Session, now: datetime, batch: int = 500) -> dict[str, int]:
    cutoff = now - timedelta(days=INACTIVE_DAYS)
    counts = {
        "listings_trimmed": 0,
        "comparables_trimmed": 0,
        "runs_deleted": 0,
        "notifications_deleted": 0,
        "details_slimmed": 0,
    }

    # listings gone for a month: drop the raw blob (the biggest column) and all but one photo
    stmt = (
        select(Listing)
        .where(Listing.is_active.is_(False), Listing.last_seen < cutoff, Listing.raw != {})
        .order_by(Listing.id)
        .limit(batch)
    )
    while rows := list(session.scalars(stmt)):
        for row in rows:
            row.raw = {}
            row.photos = row.photos[:1]
            counts["listings_trimmed"] += 1
        session.commit()

    stmt_c = (
        select(Comparable)
        .where(Comparable.is_active.is_(False), Comparable.last_seen < cutoff, Comparable.raw != {})
        .order_by(Comparable.id)
        .limit(batch)
    )
    while rows_c := list(session.scalars(stmt_c)):
        for comp in rows_c:
            comp.raw = {}
            comp.photos = comp.photos[:1]
            counts["comparables_trimmed"] += 1
        session.commit()

    # ad-page JSON stored before slim_detail existed: shrink it in place
    from app.scrapers.autoscout24_de import DETAIL_KEYS, slim_detail

    ids = list(
        session.scalars(
            select(Listing.id)
            .where(Listing.source.like("autoscout24_%"), Listing.raw.has_key("detail"))
            .order_by(Listing.id)
        )
    )
    for i in range(0, len(ids), batch):  # id chunks: a commit would close a server cursor
        chunk = ids[i : i + batch]
        for row in session.scalars(select(Listing).where(Listing.id.in_(chunk))):
            det = row.raw.get("detail") or {}
            if set(det) - set(DETAIL_KEYS) or "rawData" in (det.get("vehicle") or {}):
                row.raw = {**row.raw, "detail": slim_detail(det)}
                counts["details_slimmed"] += 1
        session.commit()

    # Automobile.it search blobs stored before slim_search existed
    from app.scrapers.automobile_it import slim_search

    ids = list(
        session.scalars(
            select(Listing.id).where(Listing.source == "automobile_it").order_by(Listing.id)
        )
    )
    for i in range(0, len(ids), batch):
        for row in session.scalars(select(Listing).where(Listing.id.in_(ids[i : i + batch]))):
            search = row.raw.get("search") or {}
            pics = search.get("pictures") or []
            if "imageRules" in search or any(len(p.get("sizes") or {}) > 1 for p in pics):
                row.raw = {**row.raw, "search": slim_search(search)}
                counts["details_slimmed"] += 1
        session.commit()

    res = session.execute(
        delete(ScrapeRun).where(ScrapeRun.started_at < now - timedelta(days=RUNS_DAYS))
    )
    counts["runs_deleted"] = res.rowcount or 0
    res = session.execute(
        delete(Notification).where(
            Notification.created_at < now - timedelta(days=NOTIFICATIONS_DAYS)
        )
    )
    counts["notifications_deleted"] = res.rowcount or 0
    session.commit()
    log.info("maintenance.prune", **counts)
    return counts
