from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.market.overview import model_detail, model_overview, overview_totals
from app.web.deps import DB, CurrentUser
from app.web.templating import templates

router = APIRouter()

SORTS = {
    "spread": lambda m: -(m.median_spread_eur if m.median_spread_eur is not None else -(10**9)),
    "positive": lambda m: -(m.positive_pct if m.positive_pct is not None else -1),
    "supply": lambda m: -m.n_active,
    "trend": lambda m: -(m.spread_change_7d if m.spread_change_7d is not None else -(10**9)),
    "best": lambda m: -((m.best.spread_eur or 0) if m.best else -(10**9)),
    "name": lambda m: m.label.lower(),
}


@router.get("/models", response_class=HTMLResponse)
def models_page(request: Request, db: DB, user: CurrentUser, sort: str = "spread") -> HTMLResponse:
    sort = sort if sort in SORTS else "spread"
    now = datetime.now(UTC)
    models = model_overview(db, now)
    models.sort(key=SORTS[sort])
    return templates.TemplateResponse(
        request,
        "models/index.html",
        {
            "title": "Models",
            "models": models,
            "totals": overview_totals(models),
            "sort": sort,
            "now": now,
        },
    )


@router.get("/models/{key}", response_class=HTMLResponse)
def model_page(request: Request, db: DB, user: CurrentUser, key: str) -> HTMLResponse:
    now = datetime.now(UTC)
    data = model_detail(db, key, now)
    if data is None:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request,
        "models/detail.html",
        {"title": data["stats"].label, "now": now, **data},
    )
