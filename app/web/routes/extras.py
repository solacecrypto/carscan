"""Watches, notes, seller pages and the digest page."""

from datetime import UTC, datetime
from statistics import median
from typing import Any

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
    return RedirectResponse(f"/listings/{listing.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/favorites", response_class=HTMLResponse)
def favorites_page(request: Request, db: DB, user: CurrentUser) -> RedirectResponse:
    return RedirectResponse("/listings?starred=true&profitable=false&include_inactive=true")


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
def digest_page(request: Request, db: DB, user: CurrentUser) -> HTMLResponse:
    digests = list(db.scalars(select(Digest).order_by(Digest.day.desc()).limit(14)))
    return templates.TemplateResponse(
        request, "digest/index.html", {"title": "Digest", "digests": digests}
    )
