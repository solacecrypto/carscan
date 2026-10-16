"""Watches, notes, seller pages and the digest page."""

from datetime import UTC, datetime
from statistics import median

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import Digest, Listing, ListingNote, Watch
from app.web.deps import DB, CurrentUser, csrf_protect
from app.web.templating import templates

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


@router.post("/listings/{listing_id}/notes", dependencies=[Depends(csrf_protect)])
def add_note(
    request: Request, db: DB, user: CurrentUser, listing_id: int, text: str = Form("")
) -> HTMLResponse:
    listing = _listing(db, listing_id)
    text = text.strip()
    if text:
        listing.notes.append(ListingNote(user=user, text=text[:2000], created_at=datetime.now(UTC)))
        db.flush()
    return _back(request, listing, "listings/_notes.html")


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
