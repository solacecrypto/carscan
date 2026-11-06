import re
from datetime import UTC, datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.alerts import notify_group
from app.db import Deal, DealEvent, DealStage, Listing, User
from app.deals import (
    ACTUAL_FIELDS,
    DATE_FIELDS,
    STAGE_ORDER,
    calibrate,
    checklist_for,
    create_deal,
    days_in_stage,
    is_stale,
    next_action,
    next_stage,
    set_stage,
    summary,
    update_actuals,
)
from app.deals.calibration import model_accuracy
from app.teams import teammates, visible_user_ids
from app.web.deps import DB, CurrentUser, csrf_protect
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(csrf_protect)])

DEAL_SORTS = {
    "updated": "last activity",
    "profit": "expected profit",
    "price": "asking price",
    "days": "longest in stage",
    "country": "country",
}


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


@router.get("/deals", response_class=HTMLResponse)
def deals_index(
    request: Request,
    db: DB,
    user: CurrentUser,
    show_closed: bool = False,
    owner: str = "",
    stage: str = "",
    country: str = "",
    source: str = "",
    q: str = "",
    sort: str = "updated",
) -> HTMLResponse:
    """The board of every deal the viewer may see: their own, or the whole team's on the
    team plan. Filters narrow by person, stage, country, source and free text."""
    people = teammates(db, user)
    stmt = (
        select(Deal)
        .where(Deal.owner_id.in_([u.id for u in people]))
        .options(selectinload(Deal.listing), selectinload(Deal.owner))
        .order_by(Deal.updated_at.desc())
    )
    all_deals = list(db.scalars(stmt))
    now = datetime.now(UTC)
    sort = sort if sort in DEAL_SORTS else "updated"
    stage = stage if stage in {st.value for st in STAGE_ORDER} else ""
    deals = _filter_deals(all_deals, owner, stage, country.upper(), source, q)
    deals = _sort_deals(deals, sort, now)
    trips = {d.id: _trip(db, d.listing, user) for d in deals}
    groups = [(st, [d for d in deals if d.stage is st]) for st in STAGE_ORDER[:-1]]
    if stage:
        groups = [(st, ds) for st, ds in groups if st.value == stage]
    sold = sorted(
        (d for d in deals if d.stage is DealStage.sold),
        key=lambda d: d.sold_at or d.stage_changed_at.date(),
        reverse=True,
    )
    dropped = [d for d in deals if d.stage is DealStage.dropped]
    filters = {"owner": owner, "stage": stage, "country": country.upper(), "source": source, "q": q}
    keep = {k: v for k, v in filters.items() if v}
    if sort != "updated":
        keep["sort"] = sort
    return templates.TemplateResponse(
        request,
        "deals/index.html",
        {
            "title": "Deals",
            "groups": groups,
            "sold": sold,
            "dropped": dropped,
            "now": now,
            "days_in_stage": days_in_stage,
            "is_stale": is_stale,
            "checklist_for": checklist_for,
            "next_action": next_action,
            "next_stage": next_stage,
            "show_closed": show_closed,
            "owner": owner,
            "owners": sorted({d.owner.username for d in all_deals}),
            "countries": sorted({d.listing.country for d in all_deals}),
            "sources": sorted({d.listing.source for d in all_deals}),
            "stages": STAGE_ORDER,
            "sorts": DEAL_SORTS,
            "sort": sort,
            "f": filters,
            "filtered": any(filters.values()),
            "qs": urlencode(keep),
            "shared": len(people) > 1,
            "counts": {u.username: sum(1 for d in all_deals if d.owner_id == u.id) for u in people},
            "trips": trips,
            "summary": summary(deals, now),
            "total": len(all_deals),
        },
    )


def _filter_deals(
    deals: list[Deal], owner: str, stage: str, country: str, source: str, q: str
) -> list[Deal]:
    words = q.lower().split()
    out = []
    for d in deals:
        x = d.listing
        if owner and d.owner.username != owner:
            continue
        if stage and d.stage.value != stage:
            continue
        if country and x.country != country:
            continue
        if source and x.source != source:
            continue
        if words:
            hay = " ".join(
                str(p) for p in (x.make, x.model, x.variant, x.city, x.country, d.notes) if p
            ).lower()
            if not all(w in hay for w in words):
                continue
        out.append(d)
    return out


def _sort_deals(deals: list[Deal], sort: str, now: datetime) -> list[Deal]:
    low = -(10**9)
    if sort == "profit":
        key = lambda d: d.expected_profit_eur if d.expected_profit_eur is not None else low  # noqa: E731
        return sorted(deals, key=key, reverse=True)
    if sort == "price":
        return sorted(deals, key=lambda d: d.purchase_price_eur or d.est_price_eur or 0)
    if sort == "days":
        return sorted(deals, key=lambda d: days_in_stage(d, now), reverse=True)
    if sort == "country":
        return sorted(deals, key=lambda d: (d.listing.country, -d.updated_at.timestamp()))
    return deals  # already newest activity first


@router.post("/deals", response_class=HTMLResponse)
def deals_create(
    request: Request, db: DB, user: CurrentUser, listing_id: str = Form(...)
) -> RedirectResponse:
    """Track a car as a deal, given our listing id or a link to its page on this site."""
    m = re.search(r"(\d+)", listing_id)
    listing = db.get(Listing, int(m.group(1))) if m else None
    if listing is None:
        raise HTTPException(404, "no such car on this site")
    deal = create_deal(db, listing, user, datetime.now(UTC))
    if deal.owner_id not in visible_user_ids(db, user):
        raise HTTPException(409, "this car is already tracked by an account outside your team")
    return RedirectResponse(f"/deals/{deal.id}", status_code=status.HTTP_303_SEE_OTHER)


def _load(db: DB, deal_id: int, user: User | None = None) -> Deal:
    """The deal, or 404 when it does not exist or belongs to someone outside the viewer's
    team (an individual account only ever sees its own deals)."""
    deal = db.scalar(
        select(Deal)
        .where(Deal.id == deal_id)
        .options(
            selectinload(Deal.listing),
            selectinload(Deal.owner),
            selectinload(Deal.events).selectinload(DealEvent.user),
        )
    )
    if deal is None or (user is not None and deal.owner_id not in visible_user_ids(db, user)):
        raise HTTPException(404)
    return deal


@router.get("/deals/{deal_id}", response_class=HTMLResponse)
def deal_detail(request: Request, db: DB, user: CurrentUser, deal_id: int) -> HTMLResponse:
    deal = _load(db, deal_id, user)
    from app.geo import nearby

    open_deals = list(
        db.scalars(
            select(Deal)
            .where(
                Deal.stage.in_([DealStage.spotted, DealStage.contacted, DealStage.inspected]),
                Deal.owner_id.in_(visible_user_ids(db, user)),
            )
            .options(selectinload(Deal.listing))
        )
    )
    near_deals = nearby(db, deal.listing, [d.listing for d in open_deals if d.id != deal.id])
    by_listing = {d.listing_id: d for d in open_deals}
    candidates = list(
        db.scalars(
            select(Listing)
            .where(
                Listing.is_active.is_(True),
                Listing.duplicate_of_id.is_(None),
                Listing.spread_eur > 0,
                Listing.country == deal.listing.country,
                Listing.id != deal.listing_id,
            )
            .order_by(Listing.spread_eur.desc())
            .limit(400)
        )
    )
    near_cars = nearby(db, deal.listing, candidates, radius_km=50)[:8]
    return templates.TemplateResponse(
        request,
        "deals/detail.html",
        {
            "title": f"Deal · {deal.listing.make} {deal.listing.model}",
            "deal": deal,
            "trip": _trip(db, deal.listing, user),
            "checklist": checklist_for(deal),
            "near_deals": [(by_listing[x.id], km) for x, km in near_deals if x.id in by_listing],
            "near_cars": near_cars,
            "stages": STAGE_ORDER,
            "actual_fields": ACTUAL_FIELDS,
            "date_fields": DATE_FIELDS,
            "now": datetime.now(UTC),
            "next_action": next_action(deal),
            "next_stage": next_stage(deal),
            "stale": is_stale(deal, datetime.now(UTC)),
            "users": teammates(db, user),
        },
    )


@router.post("/deals/{deal_id}/owner", response_class=HTMLResponse)
def deal_set_owner(
    request: Request, db: DB, user: CurrentUser, deal_id: int, owner_id: int = Form(...)
) -> RedirectResponse:
    deal = _load(db, deal_id, user)
    new_owner = db.get(User, owner_id)
    if new_owner is None or new_owner.id not in visible_user_ids(db, user):
        raise HTTPException(404)
    if new_owner.id != deal.owner_id:
        now = datetime.now(UTC)
        deal.events.append(
            DealEvent(
                user=user,
                at=now,
                kind="note",
                note=f"handed over from {deal.owner.username} to {new_owner.username}",
            )
        )
        deal.owner = new_owner
        deal.updated_at = now
        db.flush()
    return RedirectResponse(f"/deals/{deal_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/deals/{deal_id}/stage", response_class=HTMLResponse)
def deal_set_stage(
    request: Request,
    db: DB,
    user: CurrentUser,
    deal_id: int,
    stage: str = Form(...),
    note: str = Form(""),
) -> RedirectResponse:
    deal = _load(db, deal_id, user)
    try:
        new_stage = DealStage(stage)
    except ValueError:
        raise HTTPException(400, "unknown stage") from None
    if set_stage(deal, new_stage, user, datetime.now(UTC), note or None) and new_stage in (
        DealStage.bought,
        DealStage.sold,
    ):
        lst = deal.listing
        year = lst.first_registration.year if lst.first_registration else "?"
        car = f"{lst.make} {lst.model} ({year})"
        amount = deal.purchase_price_eur if new_stage is DealStage.bought else deal.sale_price_eur
        verb = "bought" if new_stage is DealStage.bought else "sold"
        money = f" for € {amount:,}".replace(",", ".") if amount else ""
        notify_group(f"{'🛒' if verb == 'bought' else '💰'} {user.username} {verb} {car}{money}")
    back = "/deals" if request.headers.get("referer", "").rstrip("/").endswith("/deals") else None
    return RedirectResponse(back or f"/deals/{deal_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/deals/{deal_id}/checklist", dependencies=[Depends(csrf_protect)])
async def deal_checklist(
    request: Request, db: DB, user: CurrentUser, deal_id: int
) -> RedirectResponse:
    deal = _load(db, deal_id, user)
    form = await request.form()
    ticked = {k for k, _label, _d in checklist_for(deal) if form.get(k) == "on"}
    deal.checklist = {k: True for k in ticked}
    deal.updated_at = datetime.now(UTC)
    db.flush()
    return RedirectResponse(f"/deals/{deal_id}#checklist", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/deals/{deal_id}/actuals", response_class=HTMLResponse)
async def deal_update_actuals(
    request: Request, db: DB, user: CurrentUser, deal_id: int
) -> RedirectResponse:
    deal = _load(db, deal_id, user)
    form = await request.form()
    data = {
        k: str(v) for k, v in form.items() if k in ACTUAL_FIELDS or k in DATE_FIELDS or k == "notes"
    }
    try:
        update_actuals(deal, data, user, datetime.now(UTC))
    except ValueError as e:
        raise HTTPException(400, f"invalid value: {e}") from None
    return RedirectResponse(f"/deals/{deal_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/calibration", response_class=HTMLResponse)
def calibration_page(request: Request, db: DB, user: CurrentUser) -> HTMLResponse:
    report = calibrate(db)
    return templates.TemplateResponse(
        request,
        "calibration.html",
        {"title": "Calibration", "report": report, "accuracy": model_accuracy(db)},
    )
