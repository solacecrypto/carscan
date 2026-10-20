from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.alerts import notify_group
from app.db import Deal, DealEvent, DealStage, Listing
from app.deals import (
    ACTUAL_FIELDS,
    DATE_FIELDS,
    STAGE_ORDER,
    calibrate,
    checklist_for,
    create_deal,
    days_in_stage,
    set_stage,
    update_actuals,
)
from app.deals.calibration import model_accuracy
from app.web.deps import DB, CurrentUser, csrf_protect
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(csrf_protect)])


def _trip(db: DB, listing: Listing) -> dict | None:  # type: ignore[type-arg]
    from app.config import get_settings
    from app.costs import get_cost_config
    from app.geo import trip

    st = get_settings()
    return trip(
        db, listing, (st.base_city, st.base_postcode, st.base_country), get_cost_config().travel
    )


@router.get("/deals", response_class=HTMLResponse)
def deals_index(
    request: Request, db: DB, user: CurrentUser, show_closed: bool = False
) -> HTMLResponse:
    stmt = (
        select(Deal)
        .options(selectinload(Deal.listing), selectinload(Deal.owner))
        .order_by(Deal.updated_at.desc())
    )
    deals = list(db.scalars(stmt))
    now = datetime.now(UTC)
    trips = {d.id: _trip(db, d.listing) for d in deals}
    groups = [
        (stage, [d for d in deals if d.stage is stage])
        for stage in STAGE_ORDER + [DealStage.dropped]
        if show_closed or stage not in (DealStage.sold, DealStage.dropped)
    ]
    return templates.TemplateResponse(
        request,
        "deals/index.html",
        {
            "title": "Deals",
            "groups": groups,
            "now": now,
            "days_in_stage": days_in_stage,
            "show_closed": show_closed,
            "trips": trips,
        },
    )


@router.post("/deals", response_class=HTMLResponse)
def deals_create(
    request: Request, db: DB, user: CurrentUser, listing_id: int = Form(...)
) -> RedirectResponse:
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(404)
    deal = create_deal(db, listing, user, datetime.now(UTC))
    return RedirectResponse(f"/deals/{deal.id}", status_code=status.HTTP_303_SEE_OTHER)


def _load(db: DB, deal_id: int) -> Deal:
    deal = db.scalar(
        select(Deal)
        .where(Deal.id == deal_id)
        .options(
            selectinload(Deal.listing),
            selectinload(Deal.owner),
            selectinload(Deal.events).selectinload(DealEvent.user),
        )
    )
    if deal is None:
        raise HTTPException(404)
    return deal


@router.get("/deals/{deal_id}", response_class=HTMLResponse)
def deal_detail(request: Request, db: DB, user: CurrentUser, deal_id: int) -> HTMLResponse:
    deal = _load(db, deal_id)
    from app.geo import nearby

    open_deals = list(
        db.scalars(
            select(Deal)
            .where(Deal.stage.in_([DealStage.spotted, DealStage.contacted, DealStage.inspected]))
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
            "trip": _trip(db, deal.listing),
            "checklist": checklist_for(deal),
            "near_deals": [(by_listing[x.id], km) for x, km in near_deals if x.id in by_listing],
            "near_cars": near_cars,
            "stages": STAGE_ORDER,
            "actual_fields": ACTUAL_FIELDS,
            "date_fields": DATE_FIELDS,
            "now": datetime.now(UTC),
        },
    )


@router.post("/deals/{deal_id}/stage", response_class=HTMLResponse)
def deal_set_stage(
    request: Request,
    db: DB,
    user: CurrentUser,
    deal_id: int,
    stage: str = Form(...),
    note: str = Form(""),
) -> RedirectResponse:
    deal = _load(db, deal_id)
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
    return RedirectResponse(f"/deals/{deal_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/deals/{deal_id}/checklist", dependencies=[Depends(csrf_protect)])
async def deal_checklist(
    request: Request, db: DB, user: CurrentUser, deal_id: int
) -> RedirectResponse:
    deal = _load(db, deal_id)
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
    deal = _load(db, deal_id)
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
