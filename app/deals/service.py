"""Deal pipeline: create from a listing, move stages, log actuals, keep history."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Deal, DealEvent, DealStage, Listing, User

STAGE_ORDER = [
    DealStage.spotted,
    DealStage.contacted,
    DealStage.inspected,
    DealStage.bought,
    DealStage.imported,
    DealStage.listed,
    DealStage.sold,
]
ACTUAL_FIELDS = (
    "purchase_price_eur",
    "transport_eur",
    "bpm_eur",
    "fees_eur",
    "repairs_eur",
    "other_eur",
    "listed_price_eur",
    "sale_price_eur",
)
DATE_FIELDS = ("bought_at", "listed_at", "sold_at")


def get_deal_for_listing(session: Session, listing_id: int) -> Deal | None:
    return session.scalar(select(Deal).where(Deal.listing_id == listing_id))


def create_deal(session: Session, listing: Listing, user: User, now: datetime) -> Deal:
    existing = get_deal_for_listing(session, listing.id)
    if existing:
        return existing
    breakdown = listing.cost_breakdown or {}
    value = listing.nl_value_breakdown or {}
    deal = Deal(
        listing=listing,
        owner=user,
        stage=DealStage.spotted,
        created_at=now,
        updated_at=now,
        stage_changed_at=now,
        est_price_eur=listing.price_eur,
        est_landed_cost_eur=listing.landed_cost_eur,
        est_bpm_eur=listing.bpm_eur,
        est_transport_eur=breakdown.get("transport_eur"),
        est_fees_eur=breakdown.get("fees_eur"),
        est_nl_value_eur=listing.nl_value_eur,
        est_nl_asking_median_eur=value.get("asking_median_eur"),
        est_spread_eur=listing.spread_eur,
    )
    deal.events.append(DealEvent(user=user, at=now, kind="created", stage=DealStage.spotted))
    session.add(deal)
    session.flush()
    return deal


def set_stage(
    deal: Deal, stage: DealStage, user: User | None, now: datetime, note: str | None = None
) -> bool:
    if deal.stage is stage:
        return False
    deal.stage = stage
    deal.stage_changed_at = now
    deal.updated_at = now
    today = now.date()
    if stage is DealStage.bought and deal.bought_at is None:
        deal.bought_at = today
    if stage is DealStage.listed and deal.listed_at is None:
        deal.listed_at = today
    if stage is DealStage.sold and deal.sold_at is None:
        deal.sold_at = today
    deal.events.append(DealEvent(user=user, at=now, kind="stage", stage=stage, note=note))
    return True


def update_actuals(
    deal: Deal, data: dict[str, Any], user: User | None, now: datetime
) -> dict[str, Any]:
    """Apply a form dict (strings allowed). Returns the changes that were actually made."""
    changes: dict[str, Any] = {}
    for f in ACTUAL_FIELDS:
        if f not in data:
            continue
        raw = data[f]
        val = (
            int(str(raw).replace(".", "").replace(",", "").strip() or 0)
            if str(raw).strip()
            else None
        )
        if getattr(deal, f) != val:
            changes[f] = [getattr(deal, f), val]
            setattr(deal, f, val)
    for f in DATE_FIELDS:
        if f not in data:
            continue
        raw = str(data[f]).strip()
        val = date.fromisoformat(raw) if raw else None
        if getattr(deal, f) != val:
            changes[f] = [getattr(deal, f).isoformat() if getattr(deal, f) else None, raw or None]
            setattr(deal, f, val)
    if "notes" in data and (data["notes"] or None) != deal.notes:
        changes["notes"] = [deal.notes, data["notes"] or None]
        deal.notes = data["notes"] or None
    if changes:
        deal.updated_at = now
        deal.events.append(DealEvent(user=user, at=now, kind="actuals", changes=changes))
    return changes


def sync_from_alert_status(
    session: Session, listing_id: int, status: str, user: User | None, now: datetime
) -> None:
    """Keep the pipeline in step with Telegram/website triage: 'contacted' advances a deal."""
    deal = get_deal_for_listing(session, listing_id)
    if deal is None:
        return
    if status == "contacted" and deal.stage is DealStage.spotted:
        set_stage(deal, DealStage.contacted, user, now, note="via alert status")
    elif status == "passed" and deal.stage in (DealStage.spotted, DealStage.contacted):
        set_stage(deal, DealStage.dropped, user, now, note="via alert status")


# what to have in hand before money changes hands; per country where the papers differ
CHECKLIST = {
    "DE": [
        ("teil1", "Zulassungsbescheinigung Teil I (Fahrzeugschein)"),
        ("teil2", "Zulassungsbescheinigung Teil II (Fahrzeugbrief)"),
        ("tuv", "Original paper TÜV report (Prüfbericht) — RDW needs it for the APK date"),
        ("coc", "COC paper (Certificate of Conformity), if the seller has it"),
        ("keys", "All keys (two) and the radio/code card"),
        ("service", "Service booklet and invoices"),
        ("vin", "VIN on the car matches the papers"),
        ("km", "Odometer matches the ad and the TÜV report"),
        ("contract", "Signed sales contract (Kaufvertrag) with seller ID details"),
        ("plates", "Export plates / insurance arranged for the drive home"),
    ],
    "BE": [
        ("inschrijving", "Inschrijvingsbewijs / certificat d'immatriculation"),
        ("keuring", "Keuringsbewijs (second-hand inspection certificate)"),
        ("coc", "Gelijkvormigheidsattest (COC)"),
        ("carpass", "Car-Pass (official mileage history)"),
        ("keys", "All keys and the radio/code card"),
        ("service", "Service booklet and invoices"),
        ("vin", "VIN on the car matches the papers"),
        ("contract", "Signed sales contract with seller ID details"),
        ("plates", "Transit plates / insurance arranged"),
    ],
    "IT": [
        ("libretto", "Libretto di circolazione"),
        ("proprieta", "Certificato di proprietà"),
        ("revisione", "Last revisione certificate"),
        ("coc", "Certificato di conformità (COC)"),
        ("keys", "All keys and the radio/code card"),
        ("service", "Tagliandi (service history)"),
        ("vin", "VIN on the car matches the papers"),
        ("contract", "Signed sales contract with seller ID details"),
        ("plates", "Export plates / insurance arranged"),
    ],
    "SE": [
        ("regbevis", "Registreringsbevis (both parts)"),
        ("besiktning", "Besiktning protocol"),
        ("coc", "COC paper, if available"),
        ("keys", "All keys and the radio/code card"),
        ("service", "Service history"),
        ("vin", "VIN on the car matches the papers"),
        ("contract", "Signed sales contract with seller ID details"),
        ("plates", "Export plates / insurance arranged"),
    ],
}


def checklist_for(deal: Deal) -> list[tuple[str, str, bool]]:
    items = CHECKLIST.get(deal.listing.country, CHECKLIST["DE"])
    done = deal.checklist or {}
    return [(key, label, bool(done.get(key))) for key, label in items]


def days_in_stage(deal: Deal, now: datetime) -> int:
    return (now - deal.stage_changed_at).days
