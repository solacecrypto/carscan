"""Gaspedaal.nl via the schema.org ItemList JSON-LD. See docs/sources/gaspedaal.md."""

import json
import re
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlencode

import structlog

from app.db.models import SellerType
from app.normalize import NormalizedListing, RawListing, to_eur
from app.scrapers.base import register
from app.scrapers.errors import ParseError
from app.scrapers.http import PoliteClient

log = structlog.get_logger()

ORIGIN = "https://www.gaspedaal.nl"
PAGE_SIZE = 100
MAX_PAGES = 20
_LD_RE = re.compile(
    r'<script type="application/ld\+json">(\{"@context":"https://schema\.org","@type":"ItemList".*?)</script>',
    re.S,
)
GEARBOX = {"Automaat": "automatic", "Handgeschakeld": "manual"}
BODY = {
    "Sedan": "sedan",
    "Stationwagon": "estate",
    "Coupé": "coupe",
    "Cabriolet": "convertible",
    "Hatchback": "hatchback",
}
FUEL = {"Benzine": "petrol", "Diesel": "diesel", "Elektrisch": "electric", "Hybride": "hybrid"}


def build_search_url(params: dict[str, Any], page: int) -> str:
    query = {k: v for k, v in params.items() if k not in ("path", "motor_regex", "bodies")}
    query.setdefault("srt", "df-a")
    if page > 1:
        query["page"] = page
    return f"{ORIGIN}{params['path']}?{urlencode(query)}"


def extract_item_list(html: str) -> dict[str, Any]:
    m = _LD_RE.search(html)
    if not m:
        raise ParseError("ItemList JSON-LD not found on Gaspedaal page")
    try:
        return json.loads(m.group(1))
    except ValueError as e:
        raise ParseError(f"invalid JSON-LD: {e}") from e


class Gaspedaal:
    name = "gaspedaal"
    country = "NL"
    headers = {"Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8"}

    def search(self, params: dict[str, Any], client: PoliteClient) -> list[RawListing]:
        motor = re.compile(params["motor_regex"], re.I) if params.get("motor_regex") else None
        bodies = set(params.get("bodies") or [])
        seen: dict[str, RawListing] = {}
        page, total_pages = 1, 1
        while page <= min(total_pages, MAX_PAGES):
            resp = client.get(build_search_url(params, page), headers=self.headers)
            if resp.status_code != 200:
                raise ParseError(f"HTTP {resp.status_code} on Gaspedaal page {page}")
            lst = extract_item_list(resp.text)
            total = int(lst.get("numberOfItems") or 0)
            total_pages = max(1, -(-total // PAGE_SIZE))
            items = [e.get("item") or {} for e in lst.get("itemListElement") or []]
            fetched_at = datetime.now(UTC)
            for item in items:
                lid = (item.get("@id") or "").rsplit("#", 1)[-1]
                if not lid or lid in seen:
                    continue
                if bodies and item.get("bodyType") not in bodies:
                    continue
                if motor and not motor.search(item.get("name") or ""):
                    continue
                seen[lid] = RawListing(
                    source=self.name,
                    source_listing_id=lid,
                    url=item.get("@id") or "",
                    data=item,
                    fetched_at=fetched_at,
                )
            log.info(
                "gaspedaal.search.page", page=page, total=total, items=len(items), kept=len(seen)
            )
            if not items:
                break
            page += 1
        return list(seen.values())

    def fetch_detail(self, raw: RawListing, client: PoliteClient) -> dict[str, Any] | None:
        return None

    def parse(self, raw: RawListing) -> NormalizedListing:
        d = raw.data
        price = (d.get("offers") or {}).get("price")
        if not price:
            raise ParseError(f"listing {raw.source_listing_id} has no price")
        name = d.get("name") or ""
        m = re.search(r"\b(\d{3}[a-z]{1,2})\b", name, re.I)
        conf = d.get("vehicleConfiguration") or ""
        kw = re.search(r"(\d+)\s*kW", conf)
        year = d.get("productionDate") or d.get("vehicleModelDate")
        seller = (d.get("offers") or {}).get("seller") or {}
        addr = seller.get("address") or {}
        mileage = (d.get("mileageFromOdometer") or {}).get("value")
        return NormalizedListing(
            source=self.name,
            source_listing_id=raw.source_listing_id,
            url=raw.url,
            make=d.get("brand") or "",
            model=m.group(1).lower() if m else (d.get("model") or "").lower(),
            variant=name,
            body_type=BODY.get(d.get("bodyType") or "", (d.get("bodyType") or "").lower() or None),
            fuel=FUEL.get(d.get("fuelType") or "", (d.get("fuelType") or "").lower() or None),
            gearbox=GEARBOX.get(d.get("vehicleTransmission") or ""),
            power_kw=int(kw.group(1)) if kw else None,
            first_registration=date(int(year), 1, 1) if year else None,
            mileage_km=int(mileage) if mileage is not None else None,
            price_eur=to_eur(price, "EUR"),
            seller_type=SellerType.dealer,
            seller_name=seller.get("name") if seller.get("name") != "Onbekende dealer" else None,
            country="NL",
            city=addr.get("addressLocality"),
            photos=[d["image"]] if d.get("image") else [],
            raw={"search": d, "year_only": True},
        )


register(Gaspedaal())
