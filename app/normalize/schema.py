"""The one schema every source is mapped into."""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

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
    # watchlist key (e.g. "vw-golf-6"): the grouping used by valuation and dedup across sites
    model_key: str | None = None
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
    # market signals some sites expose: when the ad was posted, enquiry band, reserved flag
    listed_at: date | None = None
    demand: str | None = None
    reserved: bool | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _clean(self) -> "NormalizedListing":
        from app.normalize.gdpr import clean_json, clean_text

        for f in ("variant", "description", "city", "seller_name", "make", "model"):
            v = getattr(self, f)
            if isinstance(v, str):
                object.__setattr__(self, f, clean_text(v))
        object.__setattr__(self, "raw", clean_json(self.raw))
        object.__setattr__(self, "photos", [p for p in (clean_text(x) for x in self.photos) if p])
        return self

    @property
    def title(self) -> str:
        parts = [self.make, self.model]
        if self.variant and self.variant.lower() not in self.model.lower():
            parts.append(self.variant)
        return " ".join(parts)
