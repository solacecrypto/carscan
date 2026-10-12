from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.db import Alert, AlertStatus, Listing
from app.storage.filters import apply_sql
from app.web.deps import DB, CurrentUser, csrf_protect
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(csrf_protect)])
PAGE_SIZE = 48

SORTS = {
    "newest": Listing.first_seen.desc(),
    "price_asc": Listing.price_eur.asc(),
    "price_desc": Listing.price_eur.desc(),
    "mileage": Listing.mileage_km.asc().nulls_last(),
    "year_desc": Listing.first_registration.desc().nulls_last(),
    "price_change": Listing.price_changed_at.desc().nulls_last(),
}
SortKey = Literal["newest", "price_asc", "price_desc", "mileage", "year_desc", "price_change"]


@router.get("/", response_class=HTMLResponse)
@router.get("/listings", response_class=HTMLResponse)
def listings_index(
    request: Request,
    db: DB,
    user: CurrentUser,
    price_min: int | None = Query(None, ge=0),
    price_max: int | None = Query(None, ge=0),
    mileage_max: int | None = Query(None, ge=0),
    year_min: int | None = Query(None, ge=1990),
    year_max: int | None = Query(None, ge=1990),
    gearbox: str | None = None,
    seller_type: str | None = None,
    country: str | None = None,
    new_hours: int | None = Query(None, ge=1),
    price_dropped: bool = False,
    include_inactive: bool = False,
    sort: SortKey = "newest",
    page: int = Query(1, ge=1),
) -> HTMLResponse:
    filters = {
        "price_min": price_min,
        "price_max": price_max,
        "mileage_max": mileage_max,
        "year_min": year_min,
        "year_max": year_max,
        "gearbox": gearbox or None,
        "seller_type": seller_type or None,
        "country": country or None,
    }
    stmt = apply_sql(select(Listing), filters)
    if not include_inactive:
        stmt = stmt.where(Listing.is_active.is_(True))
    if new_hours:
        stmt = stmt.where(Listing.first_seen >= datetime.now(UTC) - timedelta(hours=new_hours))
    if price_dropped:
        stmt = stmt.where(
            Listing.previous_price_eur.isnot(None), Listing.price_eur < Listing.previous_price_eur
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(
        db.scalars(
            stmt.options(selectinload(Listing.alerts).selectinload(Alert.claimed_by))
            .order_by(SORTS[sort], Listing.id.desc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
    )
    ctx = {
        "title": "Listings",
        "listings": rows,
        "total": total,
        "page": page,
        "pages": max(1, -(-total // PAGE_SIZE)),
        "sort": sort,
        "f": {
            **filters,
            "new_hours": new_hours,
            "price_dropped": price_dropped,
            "include_inactive": include_inactive,
        },
        "query": str(request.url.query),
    }
    name = "listings/_results.html" if request.headers.get("HX-Request") else "listings/index.html"
    return templates.TemplateResponse(request, name, ctx)


def _load(db: DB, listing_id: int) -> Listing:
    listing = db.scalar(
        select(Listing)
        .where(Listing.id == listing_id)
        .options(
            selectinload(Listing.alerts).selectinload(Alert.claimed_by),
            selectinload(Listing.price_history),
        )
    )
    if listing is None:
        raise HTTPException(404)
    return listing


@router.get("/listings/{listing_id}", response_class=HTMLResponse)
def listing_detail(request: Request, db: DB, user: CurrentUser, listing_id: int) -> HTMLResponse:
    listing = _load(db, listing_id)
    return templates.TemplateResponse(
        request,
        "listings/detail.html",
        {"title": listing.make + " " + listing.model, "listing": listing},
    )


@router.post("/listings/{listing_id}/status", response_class=HTMLResponse)
def listing_set_status(
    request: Request,
    db: DB,
    user: CurrentUser,
    listing_id: int,
    status_value: str = Form(..., alias="status"),
) -> HTMLResponse:
    listing = _load(db, listing_id)
    try:
        new_status = AlertStatus(status_value)
    except ValueError:
        raise HTTPException(400, "unknown status") from None
    now = datetime.now(UTC)
    if not listing.alerts:
        if not listing.saved_searches:
            raise HTTPException(409, "listing belongs to no saved search")
        listing.alerts.append(
            Alert(saved_search=listing.saved_searches[0], kind="new", sent_at=now, created_at=now)
        )
    for alert in listing.alerts:
        alert.status = new_status
        alert.claimed_by = user if new_status is not AlertStatus.new else None
        alert.status_changed_at = now
        if alert.telegram_message_id:
            alert.message_synced = False  # the worker edits the Telegram message
    db.flush()
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "listings/_status.html", {"listing": listing})
    return RedirectResponse(f"/listings/{listing_id}", status_code=status.HTTP_303_SEE_OTHER)
