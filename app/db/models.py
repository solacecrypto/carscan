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
    variant: Mapped[str | None] = mapped_column(String(255))
    body_type: Mapped[str | None] = mapped_column(String(32))
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
    cost_version: Mapped[int | None] = mapped_column(Integer)

    price_history: Mapped[list[PriceHistory]] = relationship(
        back_populates="listing", cascade="all, delete-orphan", order_by="PriceHistory.seen_at"
    )
    alerts: Mapped[list[Alert]] = relationship(
        back_populates="listing", cascade="all, delete-orphan"
    )
    saved_searches: Mapped[list[SavedSearch]] = relationship(
        secondary=listing_searches, back_populates="listings"
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
