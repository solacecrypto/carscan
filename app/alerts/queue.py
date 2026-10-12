"""Create Alert rows for things worth telling the group about. Sending happens in
app.alerts.telegram (step 7); this module never talks to Telegram."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Alert, AlertStatus, Listing, SavedSearch
from app.storage.filters import matches
from app.storage.listings import UpsertResult


def queue_alerts(
    session: Session, saved_search: SavedSearch, result: UpsertResult, now: datetime
) -> list[Alert]:
    if not saved_search.alerts_enabled:
        return []
    created: list[Alert] = []
    candidates: list[tuple[Listing, str]] = [(x, "new") for x in result.new]
    candidates += [(x, "price_drop") for x, _old in result.price_dropped]
    for listing, kind in candidates:
        if listing.duplicate_of_id is not None:
            continue  # the same car was already alerted from another source
        if not matches(listing, saved_search.filters):
            continue
        exists = session.scalar(
            select(Alert.id).where(
                Alert.listing_id == listing.id,
                Alert.saved_search_id == saved_search.id,
                Alert.kind == kind,
            )
        )
        if exists and kind == "new":
            continue
        if exists and kind == "price_drop":
            # one price_drop alert row per listing/search; re-arm it so it is sent again
            alert = session.get(Alert, exists)
            assert alert is not None
            alert.sent_at = None
            alert.telegram_message_id = None
            alert.created_at = now
            created.append(alert)
            continue
        alert = Alert(
            listing=listing,
            saved_search=saved_search,
            kind=kind,
            status=AlertStatus.new,
            created_at=now,
        )
        session.add(alert)
        created.append(alert)
    session.flush()
    return created


def pending_alerts(session: Session, limit: int = 50) -> list[Alert]:
    return list(
        session.scalars(
            select(Alert).where(Alert.sent_at.is_(None)).order_by(Alert.created_at).limit(limit)
        )
    )
