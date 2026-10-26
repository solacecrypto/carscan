"""Morning digest: the few things worth knowing before opening the site."""

from __future__ import annotations

import html
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import Deal, DealStage, Digest, Listing, ListingInsight, Notification, User

RED_FLAGS = {
    "papers_missing": "papers missing",
    "accident": "accident",
    "engine_damage": "engine damage",
}


def _car(x: Listing) -> dict[str, Any]:
    return {
        "id": x.id,
        "title": " ".join(
            str(part)
            for part in (x.make, x.model, x.first_registration.year if x.first_registration else "")
            if part
        ),
        "price_eur": x.price_eur,
        "previous_price_eur": x.previous_price_eur,
        "spread_eur": x.spread_eur,
        "country": x.country,
        "source": x.source,
    }


def build_digest(session: Session, now: datetime, hours: int = 24) -> dict[str, Any]:
    since = now - timedelta(hours=hours)
    base = (
        select(Listing)
        .options(selectinload(Listing.insight))
        .where(Listing.is_active.is_(True), Listing.duplicate_of_id.is_(None))
    )
    new = list(
        session.scalars(
            base.where(Listing.first_seen >= since, Listing.spread_eur > 0)
            .order_by(Listing.spread_eur.desc())
            .limit(10)
        )
    )
    drops = list(
        session.scalars(
            base.where(
                Listing.price_changed_at >= since,
                Listing.previous_price_eur.isnot(None),
                Listing.previous_price_eur > Listing.price_eur,
            )
            .order_by((Listing.previous_price_eur - Listing.price_eur).desc())
            .limit(5)
        )
    )
    flagged = []
    for x in session.scalars(
        base.join(ListingInsight).where(Listing.first_seen >= since, Listing.spread_eur > 0)
    ):
        keys = {t.get("key") for t in ((x.insight.tags if x.insight else None) or [])}
        hit = [RED_FLAGS[k] for k in RED_FLAGS if k in keys]
        if hit:
            flagged.append({**_car(x), "flags": hit})
    n_new = (
        session.scalar(select(func.count()).select_from(Listing).where(Listing.first_seen >= since))
        or 0
    )
    return {
        "since": since.isoformat(),
        "n_new": n_new,
        "top_new": [_car(x) for x in new],
        "price_drops": [_car(x) for x in drops],
        "flagged": flagged[:10],
    }


def _eur(v: int | None) -> str:
    return f"€ {v:,}".replace(",", ".") if v is not None else "–"


def render_text(items: dict[str, Any], site_url: str, day: datetime) -> str:
    """Telegram HTML."""
    e = html.escape
    lines = [f"☀️ <b>Morning digest · {day:%a %d %b}</b> · {items['n_new']} new cars in 24 h"]
    if items["top_new"]:
        lines.append("\n<b>Top new cars by spread</b>")
        for c in items["top_new"]:
            lines.append(
                f'• <a href="{site_url}/listings/{c["id"]}">{e(c["title"])}</a> {c["country"]} · '
                f"{_eur(c['price_eur'])} · spread <b>+{_eur(c['spread_eur'])}</b>"
            )
    if items["price_drops"]:
        lines.append("\n<b>Biggest price drops</b>")
        for c in items["price_drops"]:
            lines.append(
                f'• <a href="{site_url}/listings/{c["id"]}">{e(c["title"])}</a> '
                f"<s>{_eur(c['previous_price_eur'])}</s> → <b>{_eur(c['price_eur'])}</b>"
                + (f" · spread +{_eur(c['spread_eur'])}" if (c["spread_eur"] or 0) > 0 else "")
            )
    if items["flagged"]:
        lines.append("\n<b>Red flags on new cars</b>")
        for c in items["flagged"]:
            lines.append(
                f'• <a href="{site_url}/listings/{c["id"]}">{e(c["title"])}</a>: '
                + ", ".join(c["flags"])
            )
    if not (items["top_new"] or items["price_drops"] or items["flagged"]):
        lines.append("\nNothing worth a look today.")
    return "\n".join(lines)


def store_digest(session: Session, now: datetime, site_url: str) -> Digest:
    items = build_digest(session, now)
    text = render_text(items, site_url, now)
    row = session.scalar(select(Digest).where(Digest.day == now.date()))
    if row is None:
        row = Digest(day=now.date(), items=items, text=text, created_at=now)
        session.add(row)
    else:
        row.items, row.text, row.created_at = items, text, now
    session.flush()
    return row


FAVORITE_KINDS = (
    "price_down",
    "price_up",
    "target",
    "gone",
    "back",
    "spread_up",
    "spread_down",
    "note",
    "claimed",
)


def _flags(x: Listing) -> list[str]:
    keys = {t.get("key") for t in ((x.insight.tags if x.insight else None) or [])}
    return [RED_FLAGS[k] for k in RED_FLAGS if k in keys]


def catch_up(session: Session, user: User, now: datetime, hours: int = 24) -> dict[str, Any]:
    """Everything one person needs to be up to date, computed live: the best new cars, price
    drops, what happened to their favorites and wanted models, their deals, and which models
    moved. Returns ORM rows so the page can render full cards."""
    from app.deals.service import STAGE_ORDER, is_stale, next_action
    from app.market.overview import model_overview

    since = now - timedelta(hours=hours)
    base = (
        select(Listing)
        .options(selectinload(Listing.insight))
        .where(Listing.is_active.is_(True), Listing.duplicate_of_id.is_(None))
    )
    new_positive = list(
        session.scalars(
            base.where(Listing.first_seen >= since, Listing.spread_eur > 0)
            .order_by(Listing.spread_eur.desc())
            .limit(8)
        )
    )
    drops = list(
        session.scalars(
            base.where(
                Listing.price_changed_at >= since,
                Listing.previous_price_eur.isnot(None),
                Listing.previous_price_eur > Listing.price_eur,
            )
            .order_by(
                (Listing.spread_eur > 0).desc().nulls_last(),
                (Listing.previous_price_eur - Listing.price_eur).desc(),
            )
            .limit(6)
        )
    )
    flagged = [
        (x, _flags(x))
        for x in session.scalars(
            base.join(ListingInsight).where(Listing.first_seen >= since, Listing.spread_eur > 0)
        )
        if _flags(x)
    ][:6]
    notes = list(
        session.scalars(
            select(Notification)
            .where(Notification.user_id == user.id, Notification.created_at >= since)
            .options(selectinload(Notification.listing))
            .order_by(Notification.created_at.desc())
        )
    )
    favorites: dict[int, dict[str, Any]] = {}
    wanted: list[Notification] = []
    for n in notes:
        if n.kind in FAVORITE_KINDS and n.listing is not None:
            fav = favorites.setdefault(n.listing_id, {"listing": n.listing, "events": []})  # type: ignore[arg-type]
            fav["events"].append(n)
        elif n.kind == "wanted" and n.listing is not None:
            wanted.append(n)
    my_deals = list(
        session.scalars(
            select(Deal)
            .where(Deal.owner_id == user.id, Deal.stage.in_(STAGE_ORDER[:-1]))
            .options(selectinload(Deal.listing))
            .order_by(Deal.stage_changed_at)
        )
    )
    pre = (DealStage.spotted, DealStage.contacted, DealStage.inspected)
    deals = [
        {
            "deal": d,
            "stale": is_stale(d, now),
            "next": next_action(d),
            "gone": not d.listing.is_active and d.stage in pre,
        }
        for d in my_deals
    ]
    deals.sort(key=lambda r: (not (r["stale"] or r["gone"]), r["deal"].stage_changed_at))
    movers = [m for m in model_overview(session, now, days=14) if m.spread_change_7d is not None]
    movers.sort(key=lambda m: -(m.spread_change_7d or 0))
    counts = {
        "new": session.scalar(
            select(func.count()).select_from(Listing).where(Listing.first_seen >= since)
        )
        or 0,
        "new_positive": session.scalar(
            select(func.count())
            .select_from(Listing)
            .where(Listing.first_seen >= since, Listing.spread_eur > 0, Listing.is_active.is_(True))
        )
        or 0,
        "drops": session.scalar(
            select(func.count())
            .select_from(Listing)
            .where(
                Listing.price_changed_at >= since,
                Listing.previous_price_eur > Listing.price_eur,
                Listing.is_active.is_(True),
            )
        )
        or 0,
        "favorites": len(favorites),
        "wanted": len(wanted),
        "deals_attention": sum(1 for r in deals if r["stale"] or r["gone"]),
        "unread": sum(1 for n in notes if n.read_at is None),
    }
    return {
        "since": since,
        "hours": hours,
        "counts": counts,
        "top_new": new_positive,
        "drops": drops,
        "flagged": flagged,
        "favorites": list(favorites.values()),
        "wanted": wanted[:8],
        "deals": deals,
        "movers_up": [m for m in movers[:3] if (m.spread_change_7d or 0) > 0],
        "movers_down": [m for m in movers[-3:] if (m.spread_change_7d or 0) < 0][::-1],
    }
