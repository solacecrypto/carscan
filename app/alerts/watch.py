"""Per-car watches: tell the group when a watched car drops to the target price or disappears.
Reuses the alerts table (kinds 'watch' and 'gone') so Telegram delivery and the website's
status buttons work unchanged."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Alert, AlertStatus, Listing
from app.storage.listings import UpsertResult


def _arm(session: Session, listing: Listing, kind: str, now: datetime) -> Alert | None:
    if not listing.saved_searches:
        return None
    search = listing.saved_searches[0]
    alert = session.scalar(
        select(Alert).where(
            Alert.listing_id == listing.id,
            Alert.saved_search_id == search.id,
            Alert.kind == kind,
        )
    )
    if alert is None:
        alert = Alert(
            listing=listing, saved_search=search, kind=kind, status=AlertStatus.new, created_at=now
        )
        session.add(alert)
    else:
        alert.sent_at = None
        alert.telegram_message_id = None
        alert.created_at = now
    return alert


def check_watches(
    session: Session, result: UpsertResult, gone: list[Listing], now: datetime
) -> list[Alert]:
    """Called after every deals run: price drops against targets, and watched cars that went."""
    created: list[Alert] = []
    for listing, _old in result.price_dropped:
        hits = [
            w
            for w in listing.watches
            if w.target_price_eur is None or listing.price_eur <= w.target_price_eur
        ]
        if hits:
            alert = _arm(session, listing, "watch", now)
            if alert is not None:
                created.append(alert)
                for w in hits:
                    w.triggered_at = now
    for listing in gone:
        hits = [w for w in listing.watches if w.notify_gone]
        if hits:
            alert = _arm(session, listing, "gone", now)
            if alert is not None:
                created.append(alert)
                for w in hits:
                    w.triggered_at = now
    session.flush()
    return created
