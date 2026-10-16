from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.deals.calibration import model_accuracy
from app.market.history import model_series, sparkline
from app.web.deps import DB, CurrentUser
from app.web.templating import templates

router = APIRouter()


@router.get("/models", response_class=HTMLResponse)
def models_page(request: Request, db: DB, user: CurrentUser) -> HTMLResponse:
    from app.watchlist import load_models

    try:
        labels = {m["key"]: m["label"] for m in load_models()}
    except FileNotFoundError:
        labels = {}
    series = model_series(db)
    cards = []
    for key, rows in sorted(
        series.items(), key=lambda t: -(t[1][-1].median_spread_eur or -(10**9))
    ):
        latest = rows[-1]
        spreads = [r.median_spread_eur for r in rows]
        positive = [round(100 * r.n_positive / r.n_valued) if r.n_valued else None for r in rows]
        first = next((s for s in spreads if s is not None), None)
        cards.append(
            {
                "key": key,
                "label": labels.get(key, key),
                "latest": latest,
                "days": len(rows),
                "spread_points": sparkline(spreads),
                "positive_points": sparkline(positive),
                "positive_pct": positive[-1],
                "spread_change": (latest.median_spread_eur - first)
                if first is not None and latest.median_spread_eur is not None
                else None,
            }
        )
    return templates.TemplateResponse(
        request,
        "models/index.html",
        {
            "title": "Models",
            "cards": cards,
            "accuracy": model_accuracy(db),
            "now": datetime.now(UTC),
        },
    )
