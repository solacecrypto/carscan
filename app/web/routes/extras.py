"""Watches, notes, seller pages and the digest page."""

from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import Digest, Listing, ListingNote, Notification, PushSubscription, Subscription, Watch
from app.notify import KINDS, push, unread_count
from app.notify import matches as wanted_matches
from app.push import enabled as push_enabled
from app.web.deps import DB, CurrentUser, csrf_protect
from app.web.routes.listings import _models
from app.web.templating import STATIC_DIR, templates

router = APIRouter()


def _listing(db: DB, listing_id: int) -> Listing:
    row = db.scalar(
        select(Listing)
        .where(Listing.id == listing_id)
        .options(
            selectinload(Listing.watches).selectinload(Watch.user),
            selectinload(Listing.notes).selectinload(ListingNote.user),
        )
    )
    if row is None:
        raise HTTPException(404)
    return row


def _back(request: Request, listing: Listing, partial: str) -> HTMLResponse:
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, partial, {"listing": listing})
    if "/favorites" in request.headers.get("referer", ""):
        return RedirectResponse("/favorites", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(f"/listings/{listing.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/listings/{listing_id}/watch", dependencies=[Depends(csrf_protect)])
def watch_listing(
    request: Request,
    db: DB,
    user: CurrentUser,
    listing_id: int,
    target_price_eur: str = Form(""),
    action: str = Form("watch"),
) -> HTMLResponse:
    listing = _listing(db, listing_id)
    mine = next((w for w in listing.watches if w.user_id == user.id), None)
    if action == "unwatch":
        if mine:
            db.delete(mine)
            listing.watches.remove(mine)
    else:
        raw = target_price_eur.replace(".", "").replace(",", "").strip()
        target = int(raw) if raw.isdigit() else None
        if mine is None:
            mine = Watch(listing=listing, user=user, created_at=datetime.now(UTC))
            listing.watches.append(mine)
        mine.target_price_eur = target
        mine.triggered_at = None
    db.flush()
    return _back(request, listing, "listings/_watch.html")


@router.post("/listings/{listing_id}/star", dependencies=[Depends(csrf_protect)])
def star_listing(request: Request, db: DB, user: CurrentUser, listing_id: int) -> HTMLResponse:
    """Toggle the star; a star is a Watch row (price-drop alerts) without a target."""
    listing = _listing(db, listing_id)
    mine = next((w for w in listing.watches if w.user_id == user.id), None)
    if mine is None:
        listing.watches.append(Watch(user=user, created_at=datetime.now(UTC)))
    else:
        db.delete(mine)
        listing.watches.remove(mine)
    db.flush()
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request,
            "listings/_star.html",
            {
                "l": listing,
                "starred_ids": {listing.id} if mine is None else set(),
                "size": request.query_params.get("size", ""),
            },
        )
    if "/favorites" in request.headers.get("referer", ""):
        return RedirectResponse("/favorites", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(f"/listings/{listing.id}", status_code=status.HTTP_303_SEE_OTHER)


FAVORITE_SHOWS = ("all", "active", "gone", "drops", "profitable", "target")
FAVORITE_SORTS = ("starred", "drop", "spread", "price")


def _price_when_starred(listing: Listing, at: datetime) -> int:
    """The asking price at the moment the star was set, from the price history."""
    before = [h.price_eur for h in listing.price_history if h.seen_at <= at]
    if before:
        return before[-1]
    return listing.price_history[0].price_eur if listing.price_history else listing.price_eur


@router.get("/favorites", response_class=HTMLResponse)
def favorites_page(
    request: Request, db: DB, user: CurrentUser, show: str = "all", sort: str = "starred"
) -> HTMLResponse:
    """The user's favorite cars with what changed since they were starred."""
    from app.db import Deal

    show = show if show in FAVORITE_SHOWS else "all"
    sort = sort if sort in FAVORITE_SORTS else "starred"
    watches = list(
        db.scalars(
            select(Watch)
            .where(Watch.user_id == user.id)
            .options(
                selectinload(Watch.listing).selectinload(Listing.price_history),
                selectinload(Watch.listing).selectinload(Listing.watches).selectinload(Watch.user),
                selectinload(Watch.listing).selectinload(Listing.insight),
                selectinload(Watch.listing).selectinload(Listing.notes),
            )
            .order_by(Watch.created_at.desc())
        )
    )
    deals = {
        d.listing_id: d
        for d in db.scalars(
            select(Deal).where(Deal.listing_id.in_([w.listing_id for w in watches]))
        )
    }
    rows: list[dict[str, Any]] = []
    for w in watches:
        x = w.listing
        then = _price_when_starred(x, w.created_at)
        rows.append(
            {
                "w": w,
                "l": x,
                "price_then": then,
                "change": x.price_eur - then,
                "deal": deals.get(x.id),
                "others": [v.user.username for v in x.watches if v.user_id != user.id],
            }
        )
    active = [r for r in rows if r["l"].is_active]
    drops = [r for r in rows if r["change"] < 0]
    spreads = [r["l"].spread_eur for r in active if r["l"].spread_eur is not None]
    stats = {
        "total": len(rows),
        "active": len(active),
        "gone": len(rows) - len(active),
        "drops": len(drops),
        "saved_eur": -sum(r["change"] for r in drops),
        "rises": sum(1 for r in rows if r["change"] > 0),
        "profitable": sum(1 for r in active if (r["l"].spread_eur or 0) > 0),
        "avg_spread_eur": round(sum(spreads) / len(spreads)) if spreads else None,
        "targets": sum(1 for r in rows if r["w"].target_price_eur),
        "targets_hit": sum(
            1
            for r in rows
            if r["w"].target_price_eur and r["l"].price_eur <= r["w"].target_price_eur
        ),
        "deals": sum(1 for r in rows if r["deal"]),
    }
    if show == "active":
        rows = active
    elif show == "gone":
        rows = [r for r in rows if not r["l"].is_active]
    elif show == "drops":
        rows = drops
    elif show == "profitable":
        rows = [r for r in active if (r["l"].spread_eur or 0) > 0]
    elif show == "target":
        rows = [r for r in rows if r["w"].target_price_eur]
    if sort == "drop":
        rows.sort(key=lambda r: r["change"])
    elif sort == "spread":
        rows.sort(
            key=lambda r: r["l"].spread_eur if r["l"].spread_eur is not None else -(10**9),
            reverse=True,
        )
    elif sort == "price":
        rows.sort(key=lambda r: r["l"].price_eur)
    return templates.TemplateResponse(
        request,
        "favorites/index.html",
        {
            "title": "Favorites",
            "rows": rows,
            "stats": stats,
            "show": show,
            "sort": sort,
            "shows": FAVORITE_SHOWS,
            "starred_ids": {w.listing_id for w in watches},
        },
    )


@router.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request, db: DB, user: CurrentUser) -> HTMLResponse:
    rows = list(
        db.scalars(
            select(Notification)
            .where(Notification.user_id == user.id)
            .options(selectinload(Notification.listing))
            .order_by(Notification.read_at.is_(None).desc(), Notification.created_at.desc())
            .limit(100)
        )
    )
    subs = list(
        db.scalars(
            select(Subscription)
            .where(Subscription.user_id == user.id)
            .order_by(Subscription.created_at)
        )
    )
    return templates.TemplateResponse(
        request,
        "notifications/index.html",
        {
            "title": "Notifications",
            "rows": rows,
            "unread": unread_count(db, user.id),
            "subscriptions": subs,
            "kinds": KINDS,
            "models": _models(),
        },
    )


@router.get("/notifications/bell", response_class=HTMLResponse)
def notifications_bell(request: Request, db: DB, user: CurrentUser) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "notifications/_bell.html", {"unread": unread_count(db, user.id)}
    )


@router.post("/notifications/read", dependencies=[Depends(csrf_protect)])
def notifications_read(
    request: Request, db: DB, user: CurrentUser, notification_id: str = Form("")
) -> RedirectResponse:
    """Mark one (by id) or all notifications read."""
    now = datetime.now(UTC)
    stmt = select(Notification).where(
        Notification.user_id == user.id, Notification.read_at.is_(None)
    )
    if notification_id.isdigit():
        stmt = stmt.where(Notification.id == int(notification_id))
    for n in db.scalars(stmt):
        n.read_at = now
    db.flush()
    return RedirectResponse("/notifications", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/notifications/{notification_id}/open")
def notification_open(db: DB, user: CurrentUser, notification_id: int) -> RedirectResponse:
    n = db.get(Notification, notification_id)
    if n is None or n.user_id != user.id:
        raise HTTPException(404)
    n.read_at = n.read_at or datetime.now(UTC)
    db.flush()
    return RedirectResponse(n.url or "/notifications", status_code=status.HTTP_303_SEE_OTHER)


WANTED_TOP = 12  # cars shown per wanted model; the rest is one click away in listings


@router.get("/wanted", response_class=HTMLResponse)
def wanted_page(request: Request, db: DB, user: CurrentUser) -> HTMLResponse:
    """Every wanted model of the viewer with the cars on the market right now that match it,
    best spread first; the notifications only tell about new arrivals."""

    subs = list(
        db.scalars(
            select(Subscription)
            .where(Subscription.user_id == user.id)
            .order_by(Subscription.active.desc(), Subscription.created_at)
        )
    )
    groups = []
    for sub in subs:
        c = sub.criteria or {}
        stmt = select(Listing).where(Listing.is_active.is_(True), Listing.duplicate_of_id.is_(None))
        if c.get("model_keys"):
            stmt = stmt.where(Listing.model_key.in_(c["model_keys"]))
        if c.get("countries"):
            stmt = stmt.where(Listing.country.in_(c["countries"]))
        if c.get("max_price"):
            stmt = stmt.where(Listing.price_eur <= int(c["max_price"]))
        if c.get("positive_only") or c.get("min_spread"):
            stmt = stmt.where(Listing.spread_eur >= max(1, int(c.get("min_spread") or 0)))
        cars = [x for x in db.scalars(stmt) if wanted_matches(c, x)]
        cars.sort(
            key=lambda x: x.spread_eur if x.spread_eur is not None else -(10**9), reverse=True
        )
        new_24h = sum(1 for x in cars if x.first_seen >= datetime.now(UTC) - timedelta(hours=24))
        groups.append(
            {
                "sub": sub,
                "total": len(cars),
                "new_24h": new_24h,
                "positive": sum(1 for x in cars if x.spread_eur is not None and x.spread_eur > 0),
                "best": cars[0].spread_eur if cars and cars[0].spread_eur is not None else None,
                "cars": cars[:WANTED_TOP],
                "query": _wanted_query(c),
            }
        )
    return templates.TemplateResponse(
        request,
        "wanted/index.html",
        {
            "title": "Wanted",
            "groups": groups,
            "starred_ids": set(
                db.scalars(select(Watch.listing_id).where(Watch.user_id == user.id))
            ),
        },
    )


def _wanted_query(c: dict[str, Any]) -> str:
    """The listings filters that come closest to a subscription's criteria."""
    from urllib.parse import urlencode

    pairs: list[tuple[str, str]] = [("model_key", k) for k in c.get("model_keys", [])]
    pairs += [("country", k) for k in c.get("countries", [])]
    if c.get("query"):
        pairs.append(("q", str(c["query"])))
    if c.get("max_price"):
        pairs.append(("price_max", str(c["max_price"])))
    if c.get("max_km"):
        pairs.append(("mileage_max", str(c["max_km"])))
    if c.get("year_min"):
        pairs.append(("year_min", str(c["year_min"])))
    if c.get("min_spread"):
        pairs.append(("spread_min", str(c["min_spread"])))
    if not c.get("positive_only") and not c.get("min_spread"):
        pairs.append(("profitable", "false"))
    return urlencode(pairs)


@router.post("/wanted", dependencies=[Depends(csrf_protect)])
def wanted_create(
    request: Request,
    db: DB,
    user: CurrentUser,
    name: str = Form(""),
    model_key: list[str] = Form([]),  # noqa: B008
    query: str = Form(""),
    country: list[str] = Form([]),  # noqa: B008
    max_price: str = Form(""),
    max_km: str = Form(""),
    year_min: str = Form(""),
    min_spread: str = Form(""),
    positive_only: str = Form(""),
) -> RedirectResponse:
    def num(v: str) -> int | None:
        v = v.replace(".", "").strip()
        return int(v) if v.isdigit() else None

    criteria: dict[str, Any] = {
        k: v
        for k, v in {
            "model_keys": [m for m in model_key if m],
            "query": query.strip(),
            "countries": [c for c in country if c],
            "max_price": num(max_price),
            "max_km": num(max_km),
            "year_min": num(year_min),
            "min_spread": num(min_spread),
            "positive_only": positive_only == "true",
        }.items()
        if v
    }
    if not criteria:
        raise HTTPException(400, "give at least one criterion")
    label = name.strip() or (
        ", ".join(criteria.get("model_keys", [])) or criteria.get("query") or "any model"
    )
    db.add(
        Subscription(user=user, name=label[:120], criteria=criteria, created_at=datetime.now(UTC))
    )
    db.flush()
    return RedirectResponse("/notifications#wanted", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/wanted/{sub_id}/{action}", dependencies=[Depends(csrf_protect)])
def wanted_action(db: DB, user: CurrentUser, sub_id: int, action: str) -> RedirectResponse:
    sub = db.get(Subscription, sub_id)
    if sub is None or sub.user_id != user.id:
        raise HTTPException(404)
    if action == "delete":
        db.delete(sub)
    elif action == "toggle":
        sub.active = not sub.active
    else:
        raise HTTPException(400)
    db.flush()
    return RedirectResponse("/notifications#wanted", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/listings/{listing_id}/notes", dependencies=[Depends(csrf_protect)])
def add_note(
    request: Request, db: DB, user: CurrentUser, listing_id: int, text: str = Form("")
) -> HTMLResponse:
    listing = _listing(db, listing_id)
    text = text.strip()
    if text:
        now = datetime.now(UTC)
        listing.notes.append(ListingNote(user=user, text=text[:2000], created_at=now))
        for w in listing.watches:  # tell the others who starred this car
            if w.user_id != user.id:
                push(
                    db,
                    w.user_id,
                    "note",
                    f"{user.username} on {listing.make} {listing.model}: {text[:80]}",
                    "a car you starred",
                    now,
                    listing,
                )
        db.flush()
    return _back(request, listing, "listings/_notes.html")


NEARBY_CONDITIONS = ("accident_free", "chain_replaced", "full_history", "papers_ok", "no_defects")


def _passes_condition(x: Listing, cond: list[str]) -> bool:
    ins = x.insight
    tags = {t.get("key") for t in ((ins.tags if ins else None) or [])}
    if "accident_free" in cond and ("accident_free" not in tags or (ins and ins.accident == "yes")):
        return False
    if "chain_replaced" in cond and "chain_replaced" not in tags:
        return False
    if "full_history" in cond and "full_history" not in tags:
        return False
    if "papers_ok" in cond and "papers_missing" in tags:
        return False
    admitted = bool(x.repair_cost_eur) or bool(ins and ins.accident == "yes")
    return not ("no_defects" in cond and admitted)


@router.get("/listings/{listing_id}/nearby", response_class=HTMLResponse)
def listing_nearby(
    request: Request,
    db: DB,
    user: CurrentUser,
    listing_id: int,
    km: int = 50,
    min_spread: int = 1,
    same_model: str = "",
    page: int = 1,
) -> HTMLResponse:
    """Other cars around this one, for planning several viewings on one trip. Chips narrow
    it down: minimum spread, admitted condition, brand, same model."""
    from collections import Counter

    from sqlalchemy.orm import selectinload

    from app.geo import lookup, nearby_listings

    listing = _listing(db, listing_id)
    km = km if km in (25, 50, 100, 150, 250) else 50
    cond = [c for c in request.query_params.getlist("cond") if c in NEARBY_CONDITIONS]
    makes = [m for m in request.query_params.getlist("make") if m]
    here, found = nearby_listings(db, listing, km, positive_only=min_spread > 0, limit=400)
    if found:  # one query for the condition tags of everything in range
        ids = [x.id for x, _ in found]
        db.execute(
            select(Listing).where(Listing.id.in_(ids)).options(selectinload(Listing.insight))
        )
    total = len(found)
    brands = sorted(Counter(x.make for x, _ in found).items(), key=lambda t: (-t[1], t[0]))[:8]
    rows = []
    for x, dist in found:
        if min_spread > 1 and (x.spread_eur or 0) < min_spread:
            continue
        if makes and x.make not in makes:
            continue
        if same_model and (x.model_key or x.model) != (listing.model_key or listing.model):
            continue
        if cond and not _passes_condition(x, cond):
            continue
        rows.append((x, dist))
    per_page = 8
    total_rows = len(rows)
    pages = max(1, -(-total_rows // per_page))
    page = min(max(1, page), pages)
    page_rows = rows[(page - 1) * per_page : page * per_page]
    markers = []
    for x, dist in rows[:200]:  # the map shows the whole filtered set, the list one page
        g = lookup(db, x.city, x.postcode, x.country)
        if g and g.lat is not None:
            markers.append(
                {
                    "id": x.id,
                    "lat": g.lat,
                    "lon": g.lon,
                    "title": f"{x.make} {x.model} · € {x.price_eur:,}".replace(",", "."),
                    "km": dist,
                    "spread": x.spread_eur,
                }
            )
    starred = set(db.scalars(select(Watch.listing_id).where(Watch.user_id == user.id)))
    return templates.TemplateResponse(
        request,
        "listings/_nearby.html",
        {
            "listing": listing,
            "here": here,
            "rows": page_rows,
            "total_rows": total_rows,
            "page": page,
            "pages": pages,
            "km": km,
            "min_spread": min_spread,
            "cond": cond,
            "makes": makes,
            "same_model": bool(same_model),
            "brands": brands,
            "total": total,
            "markers": markers,
            "starred_ids": starred,
        },
    )


@router.post("/listings/{listing_id}/translate", dependencies=[Depends(csrf_protect)])
def translate_listing(request: Request, db: DB, user: CurrentUser, listing_id: int) -> HTMLResponse:
    """Translate this car's description now; the result is stored, so it is done once."""
    from app.translate import TranslateError, default_translator, translate_one

    listing = _listing(db, listing_id)
    error = None
    translator = default_translator()
    if translator is None:
        error = "translation is switched off (TRANSLATE_PROVIDER)"
    else:
        try:
            translate_one(db, listing, translator, datetime.now(UTC))
        except (TranslateError, httpx.HTTPError) as e:
            error = f"the translation service refused ({e}); try again in a few minutes"
    ctx = {"listing": listing, "translate_error": error}
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "listings/_description.html", ctx)
    return RedirectResponse(f"/listings/{listing.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/push/key")
def push_key(user: CurrentUser) -> PlainTextResponse:
    from app.config import get_settings

    return PlainTextResponse(get_settings().vapid_public_key if push_enabled() else "")


@router.post("/push/subscribe", dependencies=[Depends(csrf_protect)])
async def push_subscribe(request: Request, db: DB, user: CurrentUser) -> JSONResponse:
    data = await request.json()
    endpoint, keys = data.get("endpoint"), data.get("keys") or {}
    if not endpoint or "p256dh" not in keys or "auth" not in keys:
        raise HTTPException(400, "bad subscription")
    sub = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
    if sub is None:
        sub = PushSubscription(endpoint=endpoint, created_at=datetime.now(UTC))
        db.add(sub)
    sub.user_id, sub.keys, sub.failures = user.id, keys, 0
    sub.user_agent = (request.headers.get("user-agent") or "")[:200]
    db.flush()
    return JSONResponse({"ok": True})


@router.post("/push/unsubscribe", dependencies=[Depends(csrf_protect)])
async def push_unsubscribe(request: Request, db: DB, user: CurrentUser) -> JSONResponse:
    data = await request.json()
    sub = db.scalar(
        select(PushSubscription).where(
            PushSubscription.endpoint == data.get("endpoint"), PushSubscription.user_id == user.id
        )
    )
    if sub is not None:
        db.delete(sub)
        db.flush()
    return JSONResponse({"ok": True})


@router.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    """Served from the root so the worker's scope covers the whole site."""
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@router.get("/sellers/{name}", response_class=HTMLResponse)
def seller_page(request: Request, db: DB, user: CurrentUser, name: str) -> HTMLResponse:
    cars = list(
        db.scalars(
            select(Listing)
            .where(Listing.seller_name == name)
            .options(selectinload(Listing.insight), selectinload(Listing.alerts))
            .order_by(Listing.is_active.desc(), Listing.first_seen.desc())
        )
    )
    if not cars:
        raise HTTPException(404)
    gone = [c for c in cars if not c.is_active]
    days = [(c.last_seen - c.first_seen).days for c in gone]
    dropped = [c for c in cars if c.previous_price_eur and c.previous_price_eur > c.price_eur]
    discounts = [100 * (c.previous_price_eur - c.price_eur) / c.previous_price_eur for c in dropped]  # type: ignore[operator]
    stats = {
        "total": len(cars),
        "active": len(cars) - len(gone),
        "gone": len(gone),
        "median_days": round(median(days)) if days else None,
        "drop_share": round(100 * len(dropped) / len(cars)) if cars else 0,
        "median_discount": round(median(discounts), 1) if discounts else None,
        "sources": sorted({c.source for c in cars}),
        "cities": sorted({c.city for c in cars if c.city}),
        "positive": sum(1 for c in cars if c.is_active and (c.spread_eur or 0) > 0),
    }
    return templates.TemplateResponse(
        request,
        "sellers/detail.html",
        {"title": name, "name": name, "cars": cars, "stats": stats, "accuracy": {}},
    )


@router.get("/digest", response_class=HTMLResponse)
def digest_page(request: Request, db: DB, user: CurrentUser, hours: int = 24) -> HTMLResponse:
    """Personal catch-up page; the stored Telegram digests are kept underneath."""
    from app.digest import catch_up

    hours = hours if hours in (24, 48, 168) else 24
    now = datetime.now(UTC)
    digests = list(db.scalars(select(Digest).order_by(Digest.day.desc()).limit(14)))
    starred = set(db.scalars(select(Watch.listing_id).where(Watch.user_id == user.id)))
    return templates.TemplateResponse(
        request,
        "digest/index.html",
        {
            "title": "Digest",
            "digests": digests,
            "now": now,
            "starred_ids": starred,
            **catch_up(db, user, now, hours),
        },
    )
