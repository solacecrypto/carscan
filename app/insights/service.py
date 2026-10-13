"""Apply the analyzer to listings that have a description and no (current) insight."""

from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Listing, ListingInsight
from app.insights.analyzer import AnalysisResult, Analyzer


def description_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:32]


def pending_listings(
    session: Session, limit: int, listing_ids: list[int] | None = None
) -> list[Listing]:
    stmt = (
        select(Listing)
        .outerjoin(ListingInsight, ListingInsight.listing_id == Listing.id)
        .where(
            Listing.is_active.is_(True), Listing.description.isnot(None), Listing.description != ""
        )
        .order_by(Listing.first_seen.desc())
    )
    if listing_ids is not None:
        stmt = stmt.where(Listing.id.in_(listing_ids))
    rows = []
    for row in session.scalars(stmt):
        ins = row.insight
        if (
            ins is None
            or ins.description_hash != description_hash(row.description or "")
            or ins.status == "error"
        ):
            rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def store_result(listing: Listing, result: AnalysisResult, now: datetime) -> ListingInsight:
    ins = listing.insight
    if ins is None:
        # assign through the parent so the save-update cascade adds the row to the session
        ins = ListingInsight(analyzed_at=now, description_hash="", status="empty")
        listing.insight = ins

    ins.analyzed_at = now
    ins.description_hash = description_hash(listing.description or "")
    ins.status = result.status
    ins.model = result.model
    ins.error = result.error
    ins.raw = result.raw
    if result.insight:
        i = result.insight
        ins.language = i.language[:8]
        ins.summary = i.summary
        ins.accident = i.accident
        ins.accident_evidence = i.accident_evidence or None
        ins.timing_chain = i.timing_chain
        ins.service_history = i.service_history
        ins.export_only = i.export_only
        ins.defects = i.defects
    return ins


def analyze_pending(
    session: Session,
    analyzer: Analyzer,
    now: datetime,
    limit: int,
    listing_ids: list[int] | None = None,
) -> dict[str, int]:
    counts = {"ok": 0, "refused": 0, "error": 0}
    for listing in pending_listings(session, limit, listing_ids):
        year = listing.first_registration.year if listing.first_registration else "?"
        title = f"{listing.make} {listing.model} {listing.variant or ''} ({year})"
        result = analyzer(title, listing.description or "")
        store_result(listing, result, now)
        counts[result.status] = counts.get(result.status, 0) + 1
    session.flush()
    return counts
