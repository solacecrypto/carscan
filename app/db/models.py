from __future__ import annotations

import enum
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class SellerType(enum.StrEnum):
    dealer = "dealer"
    private = "private"


class AlertStatus(enum.StrEnum):
    new = "new"
    claimed = "claimed"
    passed = "passed"
    contacted = "contacted"


class DealStage(enum.StrEnum):
    spotted = "spotted"
    contacted = "contacted"
    inspected = "inspected"
    bought = "bought"
    imported = "imported"
    listed = "listed"
    sold = "sold"
    dropped = "dropped"


class ScrapeStatus(enum.StrEnum):
    running = "running"
    ok = "ok"
    failed = "failed"


def _enum(e: type[enum.StrEnum], name: str) -> Enum:
    return Enum(
        e, name=name, native_enum=False, length=16, values_callable=lambda x: [m.value for m in x]
    )


listing_searches = Table(
    "listing_searches",
    Base.metadata,
    Column("listing_id", ForeignKey("listings.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "saved_search_id", ForeignKey("saved_searches.id", ondelete="CASCADE"), primary_key=True
    ),
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Listing(Base):
    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("source", "source_listing_id", name="uq_listings_source_id"),
        Index("ix_listings_active_first_seen", "is_active", "first_seen"),
        Index("ix_listings_make_model", "make", "model"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    source_listing_id: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(Text)

    make: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))
    model_key: Mapped[str | None] = mapped_column(String(64), index=True)
    variant: Mapped[str | None] = mapped_column(String(255))
    body_type: Mapped[str | None] = mapped_column(String(32), index=True)
    color: Mapped[str | None] = mapped_column(String(16), index=True)
    engine_code: Mapped[str | None] = mapped_column(String(32))
    fuel: Mapped[str | None] = mapped_column(String(32))
    gearbox: Mapped[str | None] = mapped_column(String(32))
    power_kw: Mapped[int | None] = mapped_column(Integer)
    first_registration: Mapped[date | None] = mapped_column(Date)
    mileage_km: Mapped[int | None] = mapped_column(Integer)
    price_eur: Mapped[int] = mapped_column(Integer)
    vat_deductible: Mapped[bool | None] = mapped_column(Boolean)
    co2_g_km: Mapped[int | None] = mapped_column(Integer)
    vin: Mapped[str | None] = mapped_column(String(17))
    seller_type: Mapped[SellerType] = mapped_column(_enum(SellerType, "seller_type"))
    seller_name: Mapped[str | None] = mapped_column(String(255))  # dealers only
    country: Mapped[str] = mapped_column(String(2))
    city: Mapped[str | None] = mapped_column(String(128))
    postcode: Mapped[str | None] = mapped_column(String(16))
    photos: Mapped[list[str]] = mapped_column(JSONB, default=list)
    description: Mapped[str | None] = mapped_column(Text)
    description_en: Mapped[str | None] = mapped_column(Text)  # machine translation
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # convenience for the "price dropped" filter: previous price, if it ever changed downward
    previous_price_eur: Mapped[int | None] = mapped_column(Integer)
    price_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # phase 2: landed-cost estimate (see app/costs); None when it cannot be estimated
    landed_cost_eur: Mapped[int | None] = mapped_column(Integer, index=True)
    bpm_eur: Mapped[int | None] = mapped_column(Integer)
    cost_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    repair_cost_eur: Mapped[int | None] = mapped_column(Integer)  # rough, from the ad text
    repair_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    cost_version: Mapped[int | None] = mapped_column(Integer)
    # phase 4: cross-source dedup; set on the non-canonical copies of the same car
    duplicate_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("listings.id", ondelete="SET NULL"), index=True
    )

    # phase 3: NL value estimate from comparables and the spread vs landed cost
    nl_value_eur: Mapped[int | None] = mapped_column(Integer)
    nl_value_n: Mapped[int | None] = mapped_column(Integer)
    nl_value_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    spread_eur: Mapped[int | None] = mapped_column(Integer, index=True)
    spread_pct: Mapped[float | None] = mapped_column(Float)

    price_history: Mapped[list[PriceHistory]] = relationship(
        back_populates="listing", cascade="all, delete-orphan", order_by="PriceHistory.seen_at"
    )
    alerts: Mapped[list[Alert]] = relationship(
        back_populates="listing", cascade="all, delete-orphan"
    )
    saved_searches: Mapped[list[SavedSearch]] = relationship(
        secondary=listing_searches, back_populates="listings"
    )
    duplicate_of: Mapped[Listing | None] = relationship(
        remote_side="Listing.id", back_populates="duplicates", foreign_keys=[duplicate_of_id]
    )
    duplicates: Mapped[list[Listing]] = relationship(
        back_populates="duplicate_of", foreign_keys=[duplicate_of_id]
    )
    watches: Mapped[list[Watch]] = relationship(
        back_populates="listing", cascade="all, delete-orphan"
    )
    notes: Mapped[list[ListingNote]] = relationship(
        back_populates="listing", cascade="all, delete-orphan", order_by="ListingNote.created_at"
    )
    insight: Mapped[ListingInsight | None] = relationship(
        back_populates="listing", uselist=False, cascade="all, delete-orphan"
    )


class PriceHistory(Base):
    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )
    price_eur: Mapped[int] = mapped_column(Integer)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    listing: Mapped[Listing] = relationship(back_populates="price_history")


class SavedSearch(Base):
    __tablename__ = "saved_searches"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    source: Mapped[str] = mapped_column(String(32))
    source_params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    alerts_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # "deals": foreign cars we may buy (-> listings, alerts). "comparables": NL asking prices
    # used for valuation (-> comparables, no alerts).
    role: Mapped[str] = mapped_column(String(16), default="deals", server_default="deals")
    interval_minutes: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    owner: Mapped[User] = relationship()
    alerts: Mapped[list[Alert]] = relationship(back_populates="saved_search")
    listings: Mapped[list[Listing]] = relationship(
        secondary=listing_searches, back_populates="saved_searches"
    )


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint(
            "listing_id", "saved_search_id", "kind", name="uq_alerts_listing_search_kind"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )
    saved_search_id: Mapped[int] = mapped_column(ForeignKey("saved_searches.id"))
    kind: Mapped[str] = mapped_column(String(16))  # "new" | "price_drop"
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telegram_message_id: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[AlertStatus] = mapped_column(
        _enum(AlertStatus, "alert_status"), default=AlertStatus.new
    )
    claimed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    send_attempts: Mapped[int] = mapped_column(Integer, default=0)
    send_error: Mapped[str | None] = mapped_column(Text)
    # False when the status changed on the website and the Telegram message still shows the old one
    message_synced: Mapped[bool] = mapped_column(Boolean, default=True)

    listing: Mapped[Listing] = relationship(back_populates="alerts")
    saved_search: Mapped[SavedSearch] = relationship(back_populates="alerts")
    claimed_by: Mapped[User | None] = relationship()


class Comparable(Base):
    """An NL asking price used for valuation. Same shape as Listing minus deal bookkeeping."""

    __tablename__ = "comparables"
    __table_args__ = (
        UniqueConstraint("source", "source_listing_id", name="uq_comparables_source_id"),
        Index("ix_comparables_model", "make", "model", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    source_listing_id: Mapped[str] = mapped_column(String(64))
    saved_search_id: Mapped[int | None] = mapped_column(
        ForeignKey("saved_searches.id", ondelete="SET NULL"), index=True
    )
    url: Mapped[str] = mapped_column(Text)
    make: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))
    model_key: Mapped[str | None] = mapped_column(String(64), index=True)
    variant: Mapped[str | None] = mapped_column(String(255))
    body_type: Mapped[str | None] = mapped_column(String(32))
    fuel: Mapped[str | None] = mapped_column(String(32))
    gearbox: Mapped[str | None] = mapped_column(String(32))
    power_kw: Mapped[int | None] = mapped_column(Integer)
    first_registration: Mapped[date | None] = mapped_column(Date)
    mileage_km: Mapped[int | None] = mapped_column(Integer)
    price_eur: Mapped[int] = mapped_column(Integer)
    seller_type: Mapped[SellerType] = mapped_column(_enum(SellerType, "seller_type"))
    seller_name: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(128))
    photos: Mapped[list[str]] = mapped_column(JSONB, default=list)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    listed_at: Mapped[date | None] = mapped_column(Date)  # when the ad was posted (Marktplaats)
    demand: Mapped[str | None] = mapped_column(String(16))  # AutoScout24 enquiry band
    reserved: Mapped[bool | None] = mapped_column(Boolean)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Deal(Base):
    """A car someone is actually working on: pipeline stage, actual costs, outcome."""

    __tablename__ = "deals"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), unique=True
    )
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    stage: Mapped[DealStage] = mapped_column(
        _enum(DealStage, "deal_stage"), default=DealStage.spotted
    )
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stage_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # estimates frozen at creation time, for calibration later
    est_price_eur: Mapped[int] = mapped_column(Integer)
    est_landed_cost_eur: Mapped[int | None] = mapped_column(Integer)
    est_bpm_eur: Mapped[int | None] = mapped_column(Integer)
    est_transport_eur: Mapped[int | None] = mapped_column(Integer)
    est_fees_eur: Mapped[int | None] = mapped_column(Integer)
    est_nl_value_eur: Mapped[int | None] = mapped_column(Integer)
    est_nl_asking_median_eur: Mapped[int | None] = mapped_column(Integer)
    est_spread_eur: Mapped[int | None] = mapped_column(Integer)

    # actuals, all whole euros
    purchase_price_eur: Mapped[int | None] = mapped_column(Integer)
    transport_eur: Mapped[int | None] = mapped_column(Integer)
    bpm_eur: Mapped[int | None] = mapped_column(Integer)
    fees_eur: Mapped[int | None] = mapped_column(Integer)
    repairs_eur: Mapped[int | None] = mapped_column(Integer)
    other_eur: Mapped[int | None] = mapped_column(Integer)
    listed_price_eur: Mapped[int | None] = mapped_column(Integer)
    sale_price_eur: Mapped[int | None] = mapped_column(Integer)
    bought_at: Mapped[date | None] = mapped_column(Date)
    listed_at: Mapped[date | None] = mapped_column(Date)
    sold_at: Mapped[date | None] = mapped_column(Date)

    listing: Mapped[Listing] = relationship()
    owner: Mapped[User] = relationship()
    events: Mapped[list[DealEvent]] = relationship(
        back_populates="deal", cascade="all, delete-orphan", order_by="DealEvent.at"
    )

    @property
    def actual_landed_cost_eur(self) -> int | None:
        if self.purchase_price_eur is None:
            return None
        return self.purchase_price_eur + sum(
            x or 0 for x in (self.transport_eur, self.bpm_eur, self.fees_eur)
        )

    @property
    def total_cost_eur(self) -> int | None:
        landed = self.actual_landed_cost_eur
        if landed is None:
            return None
        return landed + (self.repairs_eur or 0) + (self.other_eur or 0)

    @property
    def profit_eur(self) -> int | None:
        total = self.total_cost_eur
        if total is None or self.sale_price_eur is None:
            return None
        return self.sale_price_eur - total

    @property
    def days_to_sell(self) -> int | None:
        start = self.listed_at or self.bought_at
        if start is None or self.sold_at is None:
            return None
        return (self.sold_at - start).days


class DealEvent(Base):
    __tablename__ = "deal_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("deals.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(16))  # created | stage | actuals | note
    stage: Mapped[DealStage | None] = mapped_column(_enum(DealStage, "deal_stage"))
    note: Mapped[str | None] = mapped_column(Text)
    changes: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    deal: Mapped[Deal] = relationship(back_populates="events")
    user: Mapped[User | None] = relationship()


class Watch(Base):
    """A user watching one car: alert on a price drop (optionally below a target) or when gone."""

    __tablename__ = "watches"
    __table_args__ = (UniqueConstraint("listing_id", "user_id", name="uq_watches_listing_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    target_price_eur: Mapped[int | None] = mapped_column(Integer)
    notify_gone: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    listing: Mapped[Listing] = relationship(back_populates="watches")
    user: Mapped[User] = relationship()


class ListingNote(Base):
    __tablename__ = "listing_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    listing: Mapped[Listing] = relationship(back_populates="notes")
    user: Mapped[User] = relationship()


class PushSubscription(Base):
    """One browser/device subscribed to web push for one user."""

    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    keys: Mapped[dict[str, Any]] = mapped_column(JSONB)
    user_agent: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    failures: Mapped[int] = mapped_column(Integer, default=0)


class Heartbeat(Base):
    """Last run / last success per worker job (see app.health)."""

    __tablename__ = "heartbeats"

    job: Mapped[str] = mapped_column(String(40), primary_key=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Notification(Base):
    """One in-app notification for one user (see app.notify)."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(Text)
    listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    listing: Mapped[Listing | None] = relationship()


class Subscription(Base):
    """A model (or any criteria) a user wants to hear about when a new car appears."""

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    criteria: Mapped[dict[str, Any]] = mapped_column(JSONB)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hits: Mapped[int] = mapped_column(Integer, default=0)

    user: Mapped[User] = relationship()


class Digest(Base):
    """One morning digest per day: structured items plus the Telegram text."""

    __tablename__ = "digests"

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date, unique=True)
    items: Mapped[dict[str, Any]] = mapped_column(JSONB)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GeoCache(Base):
    """Geocoded places (city/postcode/country), resolved once through Nominatim."""

    __tablename__ = "geo_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(200), unique=True)
    country: Mapped[str] = mapped_column(String(2))
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ModelHistory(Base):
    """One row per watchlist model per day: supply, valued share and median spread."""

    __tablename__ = "model_history"
    __table_args__ = (UniqueConstraint("model_key", "day", name="uq_model_history_model_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    model_key: Mapped[str] = mapped_column(String(64), index=True)
    day: Mapped[date] = mapped_column(Date)
    n_active: Mapped[int] = mapped_column(Integer)
    n_valued: Mapped[int] = mapped_column(Integer)
    n_positive: Mapped[int] = mapped_column(Integer)
    median_price_eur: Mapped[int | None] = mapped_column(Integer)
    median_landed_eur: Mapped[int | None] = mapped_column(Integer)
    median_nl_value_eur: Mapped[int | None] = mapped_column(Integer)
    median_spread_eur: Mapped[int | None] = mapped_column(Integer)
    n_comparables: Mapped[int] = mapped_column(Integer)


class ListingInsight(Base):
    """What Claude read in the seller's description (phase 6). One row per listing."""

    __tablename__ = "listing_insights"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), unique=True
    )
    status: Mapped[str] = mapped_column(String(16))  # ok | refused | error | empty
    model: Mapped[str | None] = mapped_column(String(64))
    analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    description_hash: Mapped[str] = mapped_column(String(64))
    language: Mapped[str | None] = mapped_column(String(8))
    summary: Mapped[str | None] = mapped_column(Text)
    accident: Mapped[str | None] = mapped_column(String(16))  # yes | no | unknown
    accident_evidence: Mapped[str | None] = mapped_column(Text)
    timing_chain: Mapped[str | None] = mapped_column(String(16))  # replaced | due | not_mentioned
    service_history: Mapped[str | None] = mapped_column(
        String(16)
    )  # full | partial | none | unknown
    export_only: Mapped[bool | None] = mapped_column(Boolean)
    defects: Mapped[list[str] | None] = mapped_column(JSONB)
    # condition tags [{key, label, polarity, evidence}] from the rule layer (+ Claude fields)
    tags: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    tuv_until: Mapped[date | None] = mapped_column(Date, index=True)  # inspection valid until
    photo_flags: Mapped[list[str] | None] = mapped_column(JSONB)  # what Claude saw in the photos
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)

    listing: Mapped[Listing] = relationship(back_populates="insight")


class ScrapeRun(Base):
    """One scraper execution; feeds the status page and admin alerts."""

    __tablename__ = "scrape_runs"
    __table_args__ = (Index("ix_scrape_runs_source_started", "source", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    saved_search_id: Mapped[int | None] = mapped_column(
        ForeignKey("saved_searches.id", ondelete="SET NULL")
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[ScrapeStatus] = mapped_column(_enum(ScrapeStatus, "scrape_status"))
    pages_fetched: Mapped[int] = mapped_column(Integer, default=0)
    listings_seen: Mapped[int] = mapped_column(Integer, default=0)
    listings_new: Mapped[int] = mapped_column(Integer, default=0)
    listings_price_changed: Mapped[int] = mapped_column(Integer, default=0)
    listings_deactivated: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
