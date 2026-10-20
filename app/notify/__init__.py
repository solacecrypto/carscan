"""In-app notifications: what changed on the cars you starred, and new cars that match the
models you said you are looking for. Rows live in ``notifications``; the bell in the nav
polls the unread count. Telegram stays for the group alerts."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Listing, Notification, Subscription, Watch
from app.push import send_to_user
from app.storage.listings import UpsertResult

log = structlog.get_logger()

KINDS = {
    "price_down": "price drop on a starred car",
    "price_up": "price rise on a starred car",
    "target": "starred car reached your target price",
    "gone": "starred car is gone",
    "back": "starred car is back",
    "spread_up": "starred car turned profitable",
    "spread_down": "starred car no longer profitable",
    "wanted": "new car for a wanted model",
    "note": "note on a starred car",
    "claimed": "someone claimed a starred car",
    "system": "system health",
}


def _eur(v: int | None) -> str:
    return f"€ {v:,}".replace(",", ".") if v is not None else "–"


def _title(x: Listing) -> str:
    year = x.first_registration.year if x.first_registration else ""
    return " ".join(str(p) for p in (x.make, x.model, year) if p)


def push(
    session: Session,
    user_id: int,
    kind: str,
    title: str,
    body: str | None,
    now: datetime,
    listing: Listing | None = None,
    url: str | None = None,
) -> Notification:
    """Create a notification; an unread one of the same kind for the same car is refreshed
    instead of duplicated (a car that drops three times in a day makes one entry)."""
    existing = None
    if listing is not None:
        existing = session.scalar(
            select(Notification).where(
                Notification.user_id == user_id,
                Notification.kind == kind,
                Notification.listing_id == listing.id,
                Notification.read_at.is_(None),
            )
        )
    elif kind == "system":
        existing = session.scalar(
            select(Notification).where(
                Notification.user_id == user_id,
                Notification.kind == kind,
                Notification.title == title,
                Notification.read_at.is_(None),
            )
        )
    if existing is not None:
        existing.title, existing.body, existing.created_at = title, body, now
        return existing
    try:
        send_to_user(
            session,
            user_id,
            title,
            body,
            url or (f"/listings/{listing.id}" if listing else None),
            kind,
        )
    except Exception as e:  # noqa: BLE001 - never let a push problem break the event
        log.warning("push.error", error=str(e)[:200])
    n = Notification(
        user_id=user_id,
        kind=kind,
        title=title,
        body=body,
        listing=listing,
        url=url or (f"/listings/{listing.id}" if listing else None),
        created_at=now,
    )
    session.add(n)
    return n


def _stars(listing: Listing) -> list[Watch]:
    return list(listing.watches)


def favorite_events(
    session: Session,
    result: UpsertResult,
    gone: list[Listing],
    now: datetime,
    actor_id: int | None = None,
) -> int:
    """After a scrape run: price moves, disappearances and returns of starred cars."""
    n = 0
    for listing, old in result.price_dropped:
        for w in _stars(listing):
            hit = w.target_price_eur is not None and listing.price_eur <= w.target_price_eur
            push(
                session,
                w.user_id,
                "target" if hit else "price_down",
                f"{_title(listing)}: {_eur(old)} → {_eur(listing.price_eur)}"
                + (" · at your target" if hit else ""),
                f"{listing.country} · {listing.source}"
                + (
                    f" · spread {'+' if listing.spread_eur > 0 else ''}{_eur(listing.spread_eur)}"
                    if listing.spread_eur is not None
                    else ""
                ),
                now,
                listing,
            )
            n += 1
    for listing in result.price_raised:
        for w in _stars(listing):
            push(
                session,
                w.user_id,
                "price_up",
                f"{_title(listing)}: price up to {_eur(listing.price_eur)}",
                f"was {_eur(listing.previous_price_eur)} · {listing.country} · {listing.source}",
                now,
                listing,
            )
            n += 1
    for listing in gone:
        for w in _stars(listing):
            push(
                session,
                w.user_id,
                "gone",
                f"{_title(listing)} is no longer listed",
                f"last price {_eur(listing.price_eur)} · {listing.source}",
                now,
                listing,
            )
            n += 1
    for listing in result.reactivated_listings:
        for w in _stars(listing):
            push(
                session,
                w.user_id,
                "back",
                f"{_title(listing)} is back on {listing.source}",
                f"{_eur(listing.price_eur)}",
                now,
                listing,
            )
            n += 1
    return n


def starred_spreads(session: Session, model_key: str | None) -> dict[int, int | None]:
    """Spread of every starred car (of one model) before a valuation pass."""
    stmt = select(Listing.id, Listing.spread_eur).join(Watch, Watch.listing_id == Listing.id)
    if model_key:
        stmt = stmt.where(Listing.model_key == model_key)
    return dict(session.execute(stmt).all())


def spread_events(session: Session, before: dict[int, int | None], now: datetime) -> int:
    """After a valuation pass: starred cars that crossed zero either way."""
    if not before:
        return 0
    n = 0
    for listing in session.scalars(select(Listing).where(Listing.id.in_(list(before)))):
        old, new = before.get(listing.id), listing.spread_eur
        if old is None or new is None:
            continue
        if old <= 0 < new:
            kind, title = "spread_up", f"{_title(listing)} turned profitable: +{_eur(new)}"
        elif new <= 0 < old:
            kind, title = "spread_down", f"{_title(listing)} no longer profitable: {_eur(new)}"
        else:
            continue
        for w in _stars(listing):
            push(session, w.user_id, kind, title, f"was {_eur(old)}", now, listing)
            n += 1
    return n


def matches(criteria: dict[str, Any], x: Listing) -> bool:
    """Does a new car match a wanted-model subscription?"""
    c = criteria or {}
    if c.get("model_keys") and x.model_key not in c["model_keys"]:
        return False
    if c.get("make") and (x.make or "").lower() != str(c["make"]).lower():
        return False
    if c.get("query"):
        hay = " ".join(str(p) for p in (x.make, x.model, x.variant) if p).lower()
        if not all(word in hay for word in str(c["query"]).lower().split()):
            return False
    if c.get("countries") and x.country not in c["countries"]:
        return False
    if c.get("max_price") and x.price_eur > int(c["max_price"]):
        return False
    if c.get("max_km") and (x.mileage_km is None or x.mileage_km > int(c["max_km"])):
        return False
    if c.get("year_min") and (
        x.first_registration is None or x.first_registration.year < int(c["year_min"])
    ):
        return False
    if c.get("positive_only") and not (x.spread_eur is not None and x.spread_eur > 0):
        return False
    min_spread = c.get("min_spread")
    return not (min_spread and (x.spread_eur is None or x.spread_eur < int(min_spread)))


def subscription_events(session: Session, new: list[Listing], now: datetime) -> int:
    """New cars against every active wanted-model subscription."""
    if not new:
        return 0
    subs = list(session.scalars(select(Subscription).where(Subscription.active.is_(True))))
    n = 0
    for sub in subs:
        for x in new:
            if x.duplicate_of_id is not None or not matches(sub.criteria, x):
                continue
            spread = (
                f" · spread {'+' if x.spread_eur > 0 else ''}{_eur(x.spread_eur)}"
                if x.spread_eur is not None
                else ""
            )
            push(
                session,
                sub.user_id,
                "wanted",
                f"New for “{sub.name}”: {_title(x)} · {_eur(x.price_eur)}",
                f"{x.mileage_km:,} km".replace(",", ".") + f" · {x.country} · {x.source}{spread}"
                if x.mileage_km is not None
                else f"{x.country} · {x.source}{spread}",
                now,
                x,
            )
            sub.hits += 1
            sub.last_hit_at = now
            n += 1
    return n


def unread_count(session: Session, user_id: int) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        )
        or 0
    )


def prune(session: Session, now: datetime, keep_days: int = 30) -> int:
    old = now - timedelta(days=keep_days)
    rows = list(session.scalars(select(Notification).where(Notification.created_at < old)))
    for r in rows:
        session.delete(r)
    return len(rows)
