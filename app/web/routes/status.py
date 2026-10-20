from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from app.config import get_settings
from app.db import (
    Comparable,
    Digest,
    GeoCache,
    Heartbeat,
    Listing,
    ListingInsight,
    ModelHistory,
    SavedSearch,
    ScrapeRun,
    ScrapeStatus,
)
from app.health import check
from app.scrapers import all_scrapers
from app.web.deps import DB, CurrentUser
from app.web.templating import templates

router = APIRouter()


@router.get("/status", response_class=HTMLResponse)
def status_page(request: Request, db: DB, user: CurrentUser) -> HTMLResponse:
    sources = []
    for name in sorted(all_scrapers()):
        last = db.scalar(
            select(ScrapeRun)
            .where(ScrapeRun.source == name)
            .order_by(ScrapeRun.started_at.desc())
            .limit(1)
        )
        last_ok = db.scalar(
            select(ScrapeRun)
            .where(ScrapeRun.source == name, ScrapeRun.status == ScrapeStatus.ok)
            .order_by(ScrapeRun.started_at.desc())
            .limit(1)
        )
        active = db.scalar(
            select(func.count())
            .select_from(Listing)
            .where(Listing.source == name, Listing.is_active.is_(True))
        )
        comps = db.scalar(
            select(func.count())
            .select_from(Comparable)
            .where(Comparable.source == name, Comparable.is_active.is_(True))
        )
        sources.append(
            {
                "name": name,
                "last": last,
                "last_ok": last_ok,
                "active": active or 0,
                "comps": comps or 0,
            }
        )
    analysed = (
        db.scalar(
            select(func.count()).select_from(ListingInsight).where(ListingInsight.status == "ok")
        )
        or 0
    )
    with_desc = (
        db.scalar(
            select(func.count())
            .select_from(Listing)
            .where(
                Listing.is_active.is_(True),
                Listing.description.isnot(None),
                Listing.description != "",
            )
        )
        or 0
    )
    insights = {
        "enabled": get_settings().insights_enabled,
        "analysed": analysed,
        "with_description": with_desc,
    }
    act = (Listing.is_active.is_(True), Listing.duplicate_of_id.is_(None))
    n_active = db.scalar(select(func.count()).select_from(Listing).where(*act)) or 0
    n_desc = (
        db.scalar(
            select(func.count())
            .select_from(Listing)
            .where(*act, Listing.description.isnot(None), Listing.description != "")
        )
        or 0
    )
    geo_total = db.scalar(select(func.count()).select_from(GeoCache)) or 0
    geo_ok = (
        db.scalar(select(func.count()).select_from(GeoCache).where(GeoCache.lat.isnot(None))) or 0
    )
    last_digest = db.scalar(select(func.max(Digest.day)))
    last_snapshot = db.scalar(select(func.max(ModelHistory.day)))
    pipeline = {
        "active": n_active,
        "positive": db.scalar(
            select(func.count()).select_from(Listing).where(*act, Listing.spread_eur > 0)
        )
        or 0,
        "valued": db.scalar(
            select(func.count()).select_from(Listing).where(*act, Listing.nl_value_eur.isnot(None))
        )
        or 0,
        "comparables": db.scalar(
            select(func.count()).select_from(Comparable).where(Comparable.is_active.is_(True))
        )
        or 0,
        "with_description": n_desc,
        "translated": db.scalar(
            select(func.count())
            .select_from(Listing)
            .where(*act, Listing.description_en.isnot(None))
        )
        or 0,
        "geo_ok": geo_ok,
        "geo_total": geo_total,
        "last_digest": last_digest,
        "last_snapshot": last_snapshot,
        "translate_provider": get_settings().translate_provider,
    }
    issues = check(db)
    heartbeats = list(db.scalars(select(Heartbeat).order_by(Heartbeat.job)))
    runs = list(db.scalars(select(ScrapeRun).order_by(ScrapeRun.started_at.desc()).limit(30)))
    searches = {s.id: s.name for s in db.scalars(select(SavedSearch))}
    return templates.TemplateResponse(
        request,
        "status.html",
        {
            "title": "Status",
            "sources": sources,
            "runs": runs,
            "searches": searches,
            "insights": insights,
            "pipeline": pipeline,
            "issues": issues,
            "heartbeats": heartbeats,
        },
    )
