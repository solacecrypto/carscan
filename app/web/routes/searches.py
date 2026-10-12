import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.db import SavedSearch
from app.scrapers import all_scrapers
from app.storage.filters import FILTER_KEYS
from app.web.deps import DB, CurrentUser, csrf_protect
from app.web.templating import templates

router = APIRouter(prefix="/searches", dependencies=[Depends(csrf_protect)])

# Per-source form fields for the source parameters. Adding a source = adding an entry here.
SOURCE_PARAM_FIELDS: dict[str, list[tuple[str, str, str]]] = {
    "autoscout24_nl": [
        ("path", "List path", "/lst/bmw"),
        ("cat", "Taxonomy filter (cat)", "ma13gr100038ge25mt102,ma13gr100038ge25mt103"),
        ("fuel", "Fuel code", "B"),
        ("body", "Body codes", "5,6"),
        ("fregfrom", "First registration from", "2008"),
        ("fregto", "First registration to", "2014"),
        ("cy", "Country code", "NL"),
    ],
    "autoscout24_de": [
        ("path", "List path", "/lst/bmw/3er"),
        ("cat", "Taxonomy filter (cat)", "ma13gr100038ge25mt102,ma13gr100038ge25mt103"),
        ("fuel", "Fuel code", "B"),
        ("body", "Body codes", "5,6"),
        ("fregfrom", "First registration from", "2008"),
        ("fregto", "First registration to", "2014"),
        ("cy", "Country code", "D"),
        ("gear", "Gearbox code (A/M, optional)", ""),
        ("custtype", "Seller type code (D/P, optional)", ""),
    ],
}


def _ctx(search: SavedSearch | None) -> dict[str, Any]:
    return {
        "search": search,
        "sources": sorted(all_scrapers()),
        "param_fields": SOURCE_PARAM_FIELDS,
        "filter_keys": FILTER_KEYS,
        "gearboxes": ["", "manual", "automatic"],
        "seller_types": ["", "dealer", "private"],
    }


@router.get("", response_class=HTMLResponse)
def index(request: Request, db: DB, user: CurrentUser) -> HTMLResponse:
    searches = list(db.scalars(select(SavedSearch).order_by(SavedSearch.id)))
    return templates.TemplateResponse(
        request, "searches/index.html", {"title": "Saved searches", "searches": searches}
    )


@router.get("/new", response_class=HTMLResponse)
def new(request: Request, user: CurrentUser) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "searches/form.html", {"title": "New search", **_ctx(None)}
    )


@router.get("/{search_id}/edit", response_class=HTMLResponse)
def edit(request: Request, db: DB, user: CurrentUser, search_id: int) -> HTMLResponse:
    search = db.get(SavedSearch, search_id)
    if not search:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request, "searches/form.html", {"title": "Edit search", **_ctx(search)}
    )


async def _read_form(request: Request) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    form = await request.form()
    name = str(form.get("name", "")).strip()
    source = str(form.get("source", ""))
    if not name or source not in all_scrapers():
        raise HTTPException(400, "name and a known source are required")
    params: dict[str, Any] = {}
    for key, _label, _default in SOURCE_PARAM_FIELDS.get(source, []):
        val = str(form.get(f"p_{key}", "")).strip()
        if val:
            params[key] = int(val) if val.isdigit() else val
    if not params and form.get("params_json"):
        try:
            params = json.loads(str(form["params_json"]))
        except ValueError:
            raise HTTPException(400, "params_json is not valid JSON") from None
    role = str(form.get("role", "deals"))
    if role not in ("deals", "comparables"):
        raise HTTPException(400, "unknown role")
    try:
        interval = max(5, int(str(form.get("interval_minutes", "30")) or 30))
    except ValueError:
        raise HTTPException(400, "interval must be a number") from None
    request.state.role, request.state.interval = role, interval
    filters: dict[str, Any] = {}
    for key in FILTER_KEYS:
        val = str(form.get(f"f_{key}", "")).strip()
        if val:
            filters[key] = int(val) if val.isdigit() else val
    return name, source, params, filters


@router.post("", response_class=HTMLResponse)
async def create(request: Request, db: DB, user: CurrentUser) -> RedirectResponse:
    name, source, params, filters = await _read_form(request)
    db.add(
        SavedSearch(
            name=name,
            owner=user,
            source=source,
            source_params=params,
            filters=filters,
            role=request.state.role,
            interval_minutes=request.state.interval,
            alerts_enabled=request.state.role == "deals",
        )
    )
    return RedirectResponse("/searches", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{search_id}", response_class=HTMLResponse)
async def update(request: Request, db: DB, user: CurrentUser, search_id: int) -> RedirectResponse:
    search = db.get(SavedSearch, search_id)
    if not search:
        raise HTTPException(404)
    search.name, search.source, search.source_params, search.filters = await _read_form(request)
    search.role, search.interval_minutes = request.state.role, request.state.interval
    return RedirectResponse("/searches", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{search_id}/toggle/{field}", response_class=HTMLResponse)
def toggle(request: Request, db: DB, user: CurrentUser, search_id: int, field: str) -> HTMLResponse:
    search = db.get(SavedSearch, search_id)
    if not search or field not in ("active", "alerts_enabled"):
        raise HTTPException(404)
    setattr(search, field, not getattr(search, field))
    db.flush()
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "searches/_row.html", {"s": search})
    return RedirectResponse("/searches", status_code=status.HTTP_303_SEE_OTHER)
