"""The one schema every source is mapped into."""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field

from app.db.models import SellerType


class RawListing(BaseModel):
    """What a scraper returns from `search`: the source's own JSON, already GDPR-scrubbed."""

    source: str
    source_listing_id: str
    url: str
    data: dict[str, Any]
    detail: dict[str, Any] | None = None
    fetched_at: datetime


class NormalizedListing(BaseModel):
    source: str
    source_listing_id: str
    url: str
    make: str
    model: str
    variant: str | None = None
    body_type: str | None = None
    engine_code: str | None = None
    fuel: str | None = None
    gearbox: str | None = None
    power_kw: int | None = None
    first_registration: date | None = None
    mileage_km: int | None = None
    price_eur: int
    vat_deductible: bool | None = None
    co2_g_km: int | None = None
    vin: str | None = None
    seller_type: SellerType
    seller_name: str | None = None
    country: str = Field(min_length=2, max_length=2)
    city: str | None = None
    postcode: str | None = None
    photos: list[str] = Field(default_factory=list)
    description: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def title(self) -> str:
        parts = [self.make, self.model]
        if self.variant and self.variant.lower() not in self.model.lower():
            parts.append(self.variant)
        return " ".join(parts)
