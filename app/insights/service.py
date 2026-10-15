"""Apply the analyzer to listings that have a description and no (current) insight."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Listing, ListingInsight
from app.insights.analyzer import AnalysisResult, Analyzer
from app.insights.rules import extract_tags, summarize

RULE_STATUSES = ("rules", "empty", "error")


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
            or ins.status in RULE_STATUSES
        ):
            rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def apply_rules(listing: Listing, now: datetime) -> ListingInsight | None:
    """Rule-based tags for a listing's description; keeps a Claude result if one is current."""
    text = listing.description or ""
    if not text.strip():
        return None
    ins = listing.insight
    digest = description_hash(text)
    if ins is not None and ins.status == "ok" and ins.description_hash == digest:
        return ins
    if ins is None:
        ins = ListingInsight(analyzed_at=now, description_hash=digest, status="rules")
        listing.insight = ins
    tags = extract_tags(text)
    fields = summarize(tags)
    ins.analyzed_at = now
    ins.description_hash = digest
    ins.status = "rules"
    ins.model = "rules"
    ins.error = None
    ins.tags = [t.as_dict() for t in tags]
    ins.accident = fields["accident"]
    ins.accident_evidence = fields["accident_evidence"]
    ins.timing_chain = fields["timing_chain"]
    ins.service_history = fields["service_history"]
    ins.export_only = fields["export_only"]
    ins.defects = fields["defects"]
    ins.summary = None
    return ins


def _claude_tags(i, rule_tags: list[dict[str, Any]]) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    """Claude's verdicts as tags, keeping the rule layer's evidence where keys overlap."""
    by_key = {t["key"]: t for t in rule_tags}
    out: list[dict[str, Any]] = []

    def add(key: str, label: str, pol: str, evidence: str | None = None) -> None:
        ev = evidence or by_key.get(key, {}).get("evidence") or ""
        out.append({"key": key, "label": label, "polarity": pol, "evidence": ev})

    if i.accident == "yes":
        add("accident", "accident", "bad", i.accident_evidence)
    elif i.accident == "no":
        add("accident_free", "accident-free", "good", i.accident_evidence)
    for d in i.defects:
        out.append({"key": "defect", "label": d, "polarity": "bad", "evidence": ""})
    if i.export_only:
        add("export_only", "export / trade only", "bad")
    if i.timing_chain == "replaced":
        add("chain_replaced", "chain replaced", "good")
    elif i.timing_chain == "due":
        add("chain_due", "chain due", "bad")
    if i.service_history == "full":
        add("full_history", "full service history", "good")
    elif i.service_history == "none":
        add("no_history", "no service history", "bad")
    for t in rule_tags:  # neutral notes from the rules survive
        if t["polarity"] == "note" and t["key"] not in {o["key"] for o in out}:
            out.append(t)
    order = {"bad": 0, "good": 1, "note": 2}
    return sorted(out, key=lambda t: order[t["polarity"]])


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
        ins.tags = _claude_tags(i, [t.as_dict() for t in extract_tags(listing.description)])
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
