from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import Select, func, select
from sqlalchemy.orm import selectinload

from app.db import Alert, AlertStatus, Comparable, Listing
from app.deals import get_deal_for_listing, sync_from_alert_status
from app.deals.calibration import model_accuracy
from app.market import market_for_listing
from app.normalize.attrs import BODIES, COLORS
from app.storage.filters import apply_sql, parse_filters
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
    "landed_asc": Listing.landed_cost_eur.asc().nulls_last(),
    "spread_desc": Listing.spread_eur.desc().nulls_last(),
}


@router.get("/", response_class=HTMLResponse)
@router.get("/listings", response_class=HTMLResponse)
def listings_index(request: Request, db: DB, user: CurrentUser) -> HTMLResponse:
    models = _models()
    f = parse_filters(request.query_params, models)
    sort = request.query_params.get("sort") or "newest"
    if sort not in SORTS:
        sort = "newest"
    try:
        page = max(1, int(request.query_params.get("page") or 1))
    except ValueError:
        page = 1
    stmt = _filtered_stmt(f)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(
        db.scalars(
            stmt.options(
                selectinload(Listing.alerts).selectinload(Alert.claimed_by),
                selectinload(Listing.duplicates),
                selectinload(Listing.insight),
            )
            .order_by(SORTS[sort], Listing.id.desc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
    )
    ctx = {
        "title": "Listings",
        "listings": rows,
        "brands": _brands(models),
        "models": models,
        "model_makes": _model_makes(),
        "since": datetime.now(UTC).isoformat(),
        "total": total,
        "page": page,
        "pages": max(1, -(-total // PAGE_SIZE)),
        "sort": sort,
        "f": f,
        "query": _clean_query(request),
        "accuracy": model_accuracy(db),
        "bodies": BODIES,
        "colors": COLORS,
        "swatches": SWATCHES,
    }
    if request.headers.get("HX-Request"):
        resp = templates.TemplateResponse(request, "listings/_results.html", ctx)
        clean = ctx["query"]
        resp.headers["HX-Push-Url"] = "/listings" + (f"?{clean}" if clean else "")
        return resp
    return templates.TemplateResponse(request, "listings/index.html", ctx)


def _clean_query(request: Request) -> str:
    """The query string without empty values, so pushed URLs and the poller stay short."""
    pairs = [(k, v) for k, v in request.query_params.multi_items() if v not in ("", None)]
    return urlencode(pairs)


def _filtered_stmt(f: dict) -> Select:  # type: ignore[type-arg]
    stmt = apply_sql(select(Listing), f)
    if not f.get("include_inactive"):
        stmt = stmt.where(Listing.is_active.is_(True))
    if not f.get("include_duplicates"):
        stmt = stmt.where(Listing.duplicate_of_id.is_(None))
    if f.get("new_hours"):
        stmt = stmt.where(
            Listing.first_seen >= datetime.now(UTC) - timedelta(hours=int(f["new_hours"]))
        )
    if f.get("price_dropped"):
        stmt = stmt.where(
            Listing.previous_price_eur.isnot(None), Listing.price_eur < Listing.previous_price_eur
        )
    return stmt


@router.get("/listings/new-count", response_class=HTMLResponse)
def listings_new_count(request: Request, db: DB, user: CurrentUser, since: str) -> HTMLResponse:
    """Polled by the listings page: cars matching the current filters that arrived after `since`."""
    try:
        since_dt = datetime.fromisoformat(since.replace(" ", "+"))  # '+' arrives as a space
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=UTC)
    except ValueError:
        raise HTTPException(400, "bad since") from None
    f = parse_filters(request.query_params, _models())
    f["new_hours"] = None
    f["price_dropped"] = False
    stmt = _filtered_stmt(f).where(Listing.first_seen > since_dt)
    count = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    query = "&".join(
        p for p in _clean_query(request).split("&") if p and not p.startswith("since=")
    )
    return templates.TemplateResponse(
        request, "listings/_poller.html", {"count": count, "since": since, "query": query}
    )


def _brands(models: list[tuple[str, str]]) -> list[str]:
    from app.watchlist import load_models

    try:
        return sorted({m["make"] for m in load_models()})
    except FileNotFoundError:
        return []


def _model_makes() -> dict[str, str]:
    from app.watchlist import load_models

    try:
        return {m["key"]: m["make"] for m in load_models()}
    except FileNotFoundError:
        return {}


def _models() -> list[tuple[str, str]]:
    from app.watchlist import load_models

    try:
        return [(m["key"], m["label"]) for m in load_models()]
    except FileNotFoundError:
        return []


def _load(db: DB, listing_id: int) -> Listing:
    listing = db.scalar(
        select(Listing)
        .where(Listing.id == listing_id)
        .options(
            selectinload(Listing.alerts).selectinload(Alert.claimed_by),
            selectinload(Listing.price_history),
            selectinload(Listing.duplicates),
            selectinload(Listing.duplicate_of),
            selectinload(Listing.insight),
        )
    )
    if listing is None:
        raise HTTPException(404)
    return listing


@router.get("/listings/{listing_id}", response_class=HTMLResponse)
def listing_detail(request: Request, db: DB, user: CurrentUser, listing_id: int) -> HTMLResponse:
    listing = _load(db, listing_id)
    nearest_ids = [n["id"] for n in (listing.nl_value_breakdown or {}).get("nearest", [])]
    comps = (
        {c.id: c for c in db.scalars(select(Comparable).where(Comparable.id.in_(nearest_ids)))}
        if nearest_ids
        else {}
    )
    return templates.TemplateResponse(
        request,
        "listings/detail.html",
        {
            "title": listing.make + " " + listing.model,
            "listing": listing,
            "nearest": [comps[i] for i in nearest_ids if i in comps],
            "deal": get_deal_for_listing(db, listing.id),
            "market": market_for_listing(db, listing, datetime.now(UTC)),
            "trip": _trip(db, listing),
        },
    )


SWATCHES = {
    "black": "#111", "grey": "#8a8f98", "silver": "#c9ccd1", "white": "#f4f4f4", "blue": "#2457c5",
    "red": "#c22", "green": "#2a8a3e", "brown": "#6b4a2b", "beige": "#d8c9a5", "yellow": "#e6c21a",
    "orange": "#e5731a", "gold": "#c9a227", "purple": "#6f3fa0", "other": "#b0b0b0",
}  # fmt: skip


def _trip(db: DB, listing: Listing) -> dict | None:  # type: ignore[type-arg]
    from app.config import get_settings
    from app.costs import get_cost_config
    from app.geo import trip

    st = get_settings()
    return trip(
        db, listing, (st.base_city, st.base_postcode, st.base_country), get_cost_config().travel
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
    sync_from_alert_status(db, listing.id, new_status.value, user, now)
    db.flush()
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "listings/_status.html", {"listing": listing})
    return RedirectResponse(f"/listings/{listing_id}", status_code=status.HTTP_303_SEE_OTHER)
