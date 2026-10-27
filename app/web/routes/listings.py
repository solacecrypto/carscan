import csv
import io
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import Select, func, select
from sqlalchemy.orm import selectinload

from app.db import Alert, AlertStatus, Comparable, FilterPreset, Listing, Watch
from app.deals import get_deal_for_listing, sync_from_alert_status
from app.deals.calibration import model_accuracy
from app.market import market_for_listing
from app.normalize.attrs import BODIES, COLORS
from app.notify import push
from app.storage.filters import apply_sql, parse_filters
from app.web.deps import DB, CurrentUser, csrf_protect
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(csrf_protect)])
PAGE_SIZE = 48
EXPORT_LIMIT = 2000  # rows in a CSV export; the same filters as the page, no paging

CSV_COLUMNS = (
    "id", "source", "make", "model", "variant", "year", "mileage_km", "fuel", "gearbox",
    "power_kw", "body", "color", "price_eur", "previous_price_eur", "landed_cost_eur",
    "bpm_eur", "repair_cost_eur", "nl_value_eur", "spread_eur", "spread_pct", "country",
    "city", "seller_type", "vat_deductible", "first_seen", "last_seen", "active", "ad_url",
    "carscan_url",
)  # fmt: skip

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
    here = _here(db, user) if f.get("max_km") else None
    stmt = _filtered_stmt(f, here)
    if f.get("starred"):
        stmt = stmt.join(Watch, (Watch.listing_id == Listing.id) & (Watch.user_id == user.id))
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
        "starred_ids": set(db.scalars(select(Watch.listing_id).where(Watch.user_id == user.id))),
        "here_missing": bool(f.get("max_km")) and here is None,
        "presets": list(
            db.scalars(
                select(FilterPreset)
                .where(FilterPreset.user_id == user.id)
                .order_by(FilterPreset.created_at)
            )
        ),
    }
    if request.headers.get("HX-Request"):
        resp = templates.TemplateResponse(request, "listings/_results.html", ctx)
        clean = ctx["query"]
        resp.headers["HX-Push-Url"] = "/listings" + (f"?{clean}" if clean else "")
        return resp
    return templates.TemplateResponse(request, "listings/index.html", ctx)


@router.get("/listings.csv")
def listings_export(request: Request, db: DB, user: CurrentUser) -> Response:
    """The filtered list as CSV (same filters and sort as the page, up to EXPORT_LIMIT rows),
    for sharing a shortlist outside the group or working in a spreadsheet."""
    f = parse_filters(request.query_params, _models())
    sort = request.query_params.get("sort") or "newest"
    here = _here(db, user) if f.get("max_km") else None
    stmt = _filtered_stmt(f, here)
    if f.get("starred"):
        stmt = stmt.join(Watch, (Watch.listing_id == Listing.id) & (Watch.user_id == user.id))
    rows = db.scalars(
        stmt.order_by(SORTS.get(sort, SORTS["newest"]), Listing.id.desc()).limit(EXPORT_LIMIT)
    )
    base = str(request.base_url).rstrip("/")
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")  # Excel in NL opens ';' files as columns straight away
    w.writerow(CSV_COLUMNS)
    for x in rows:
        w.writerow(
            [
                x.id,
                x.source,
                x.make,
                x.model,
                x.variant or "",
                x.first_registration.year if x.first_registration else "",
                x.mileage_km if x.mileage_km is not None else "",
                x.fuel or "",
                x.gearbox or "",
                x.power_kw if x.power_kw is not None else "",
                x.body_type or "",
                x.color or "",
                x.price_eur,
                x.previous_price_eur if x.previous_price_eur is not None else "",
                x.landed_cost_eur if x.landed_cost_eur is not None else "",
                x.bpm_eur if x.bpm_eur is not None else "",
                x.repair_cost_eur if x.repair_cost_eur is not None else "",
                x.nl_value_eur if x.nl_value_eur is not None else "",
                x.spread_eur if x.spread_eur is not None else "",
                round(x.spread_pct, 1) if x.spread_pct is not None else "",
                x.country,
                x.city or "",
                x.seller_type.value,
                "" if x.vat_deductible is None else ("yes" if x.vat_deductible else "no"),
                x.first_seen.strftime("%Y-%m-%d %H:%M"),
                x.last_seen.strftime("%Y-%m-%d %H:%M"),
                "yes" if x.is_active else "no",
                x.url,
                f"{base}/listings/{x.id}",
            ]  # fmt: skip
        )
    name = f"carscan-{datetime.now(UTC):%Y%m%d-%H%M}.csv"
    return Response(
        "\ufeff" + buf.getvalue(),  # BOM so Excel reads the UTF-8 (ë, ü) correctly
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post("/listings/star-bulk", dependencies=[Depends(csrf_protect)])
def star_bulk(
    db: DB,
    user: CurrentUser,
    ids: str = Form(""),
    op: str = Form("add"),
    next: str = Form("/listings"),  # noqa: A002
) -> RedirectResponse:
    """Favorite or unfavorite the selected cars in one go (checkboxes on the listings page)."""
    wanted = {int(v) for v in ids.replace(",", " ").split() if v.isdigit()}
    if not wanted:
        raise HTTPException(400, "select at least one car")
    mine = {
        w.listing_id: w
        for w in db.scalars(
            select(Watch).where(Watch.user_id == user.id, Watch.listing_id.in_(wanted))
        )
    }
    now = datetime.now(UTC)
    if op == "remove":
        for w in mine.values():
            db.delete(w)
    else:
        existing = set(db.scalars(select(Listing.id).where(Listing.id.in_(wanted))))
        for lid in existing - mine.keys():
            db.add(Watch(user_id=user.id, listing_id=lid, created_at=now))
    db.flush()
    if not next.startswith("/") or next.startswith("//"):
        next = "/listings"  # noqa: A001
    return RedirectResponse(next, status_code=status.HTTP_303_SEE_OTHER)


def _clean_query(request: Request) -> str:
    """The query string without empty values, so pushed URLs and the poller stay short."""
    pairs = [(k, v) for k, v in request.query_params.multi_items() if v not in ("", None)]
    return urlencode(pairs)


def _here(db: DB, user: object) -> tuple[float, float] | None:
    """The viewer's point for the distance filter: their home, else the configured base."""
    from app.config import get_settings
    from app.geo import base_point

    st = get_settings()
    pt = base_point(db, user, (st.base_city, st.base_postcode, st.base_country))
    return (pt[0], pt[1]) if pt else None


def _filtered_stmt(f: dict, here: tuple[float, float] | None = None) -> Select:  # type: ignore[type-arg]
    from app.costs import get_cost_config

    detour = float((get_cost_config().travel or {}).get("detour_factor", 1.25))
    stmt = apply_sql(select(Listing), f, here, detour)
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


@router.post("/presets", dependencies=[Depends(csrf_protect)])
def preset_save(
    db: DB, user: CurrentUser, name: str = Form(""), query: str = Form("")
) -> RedirectResponse:
    """Save the current filters under a name (the listings query string, minus paging)."""
    name = name.strip()[:60]
    pairs = [
        (k, v)
        for k, v in parse_qsl(query.lstrip("?"), keep_blank_values=False)
        if k not in ("page", "since") and v
    ]
    clean = urlencode(pairs)
    if not name or not clean:
        raise HTTPException(400, "a name and at least one filter are needed")
    existing = db.scalar(
        select(FilterPreset).where(FilterPreset.user_id == user.id, FilterPreset.name == name)
    )
    if existing is not None:
        existing.query = clean
    else:
        db.add(FilterPreset(user_id=user.id, name=name, query=clean, created_at=datetime.now(UTC)))
    db.flush()
    return RedirectResponse(f"/listings?{clean}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/presets/{preset_id}/delete", dependencies=[Depends(csrf_protect)])
def preset_delete(db: DB, user: CurrentUser, preset_id: int) -> RedirectResponse:
    row = db.get(FilterPreset, preset_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(404)
    db.delete(row)
    db.flush()
    return RedirectResponse("/listings", status_code=status.HTTP_303_SEE_OTHER)


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
    here = _here(db, user) if f.get("max_km") else None
    stmt = _filtered_stmt(f, here).where(Listing.first_seen > since_dt)
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
            "trip": _trip(db, listing, user),
            "coords": _coords(db, listing),
            "starred_ids": {w.listing_id for w in listing.watches if w.user_id == user.id},
        },
    )


SWATCHES = {
    "black": "#111", "grey": "#8a8f98", "silver": "#c9ccd1", "white": "#f4f4f4", "blue": "#2457c5",
    "red": "#c22", "green": "#2a8a3e", "brown": "#6b4a2b", "beige": "#d8c9a5", "yellow": "#e6c21a",
    "orange": "#e5731a", "gold": "#c9a227", "purple": "#6f3fa0", "other": "#b0b0b0",
}  # fmt: skip


def _coords(db: DB, listing: Listing) -> dict | None:  # type: ignore[type-arg]
    from app.geo import lookup

    g = lookup(db, listing.city, listing.postcode, listing.country)
    if g is None or g.lat is None:
        return None
    return {"lat": g.lat, "lon": g.lon}


def _trip(db: DB, listing: Listing, user: object = None) -> dict | None:  # type: ignore[type-arg]
    from app.config import get_settings
    from app.costs import get_cost_config
    from app.geo import trip

    st = get_settings()
    return trip(
        db,
        listing,
        (st.base_city, st.base_postcode, st.base_country),
        get_cost_config().travel,
        user,
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
    if new_status in (AlertStatus.claimed, AlertStatus.contacted):
        for w in listing.watches:
            if w.user_id != user.id:
                push(
                    db,
                    w.user_id,
                    "claimed",
                    f"{user.username} marked {listing.make} {listing.model} as {new_status.value}",
                    "a car you starred",
                    now,
                    listing,
                )
    db.flush()
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "listings/_status.html", {"listing": listing})
    return RedirectResponse(f"/listings/{listing_id}", status_code=status.HTTP_303_SEE_OTHER)
