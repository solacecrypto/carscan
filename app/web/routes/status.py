from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from app.config import get_settings
from app.db import Comparable, Listing, ListingInsight, SavedSearch, ScrapeRun, ScrapeStatus
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
        },
    )
