"""Marktplaats.nl via its internal JSON search API. See docs/sources/marktplaats.md."""

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import structlog

from app.db.models import SellerType
from app.normalize import NormalizedListing, RawListing, scrub, scrub_text, to_eur
from app.scrapers.base import register
from app.scrapers.errors import ParseError
from app.scrapers.http import PoliteClient

log = structlog.get_logger()

ORIGIN = "https://www.marktplaats.nl"
PAGE_SIZE = 30
USABLE_PRICE_TYPES = {"FIXED", "MIN_BID"}
GEARBOX = {"Automaat": "automatic", "Handgeschakeld": "manual", "Semi-automaat": "semi-automatic"}
BODY = {
    "Sedan": "sedan",
    "Berline": "sedan",
    "Stationwagon": "estate",
    "Break": "estate",
    "Coupé": "coupe",
    "Cabriolet": "convertible",
    "Hatchback (3/5-deurs)": "hatchback",
    "SUV of Terreinwagen": "suv",
    "MPV": "van",
}
FUEL = {
    "Benzine": "petrol",
    "Diesel": "diesel",
    "Elektrisch": "electric",
    "Hybride": "hybrid",
    "LPG": "lpg",
}


def build_api_url(params: dict[str, Any], offset: int, origin: str = ORIGIN) -> str:
    """params: l1CategoryId, l2CategoryId, model, fuel, year_from, year_to, query, mileage_max."""
    q: list[tuple[str, Any]] = [
        ("l1CategoryId", params.get("l1CategoryId", 91)),
        ("l2CategoryId", params["l2CategoryId"]),
        ("limit", PAGE_SIZE),
        ("offset", offset),
        ("sortBy", "SORT_INDEX"),
        ("sortOrder", "DECREASING"),
    ]
    if params.get("model"):
        q.append(("attributesByKey[]", f"model:{params['model']}"))
    if params.get("fuel"):
        q.append(("attributesByKey[]", f"fuel:{params['fuel']}"))
    if params.get("year_from") or params.get("year_to"):
        q.append(
            (
                "attributeRanges[]",
                f"constructionYear:{params.get('year_from', '')}:{params.get('year_to', '')}",
            )
        )
    if params.get("mileage_max"):
        q.append(("attributeRanges[]", f"mileage:0:{params['mileage_max']}"))
    if params.get("query"):
        q.append(("query", params["query"]))
    return f"{origin}/lrp/api/search?{urlencode(q)}"


_NL_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]
    )
}
_RELATIVE = {"vandaag": 0, "gisteren": 1, "eergisteren": 2}


def parse_listed_date(text: str | None, today: date) -> date | None:
    """Marktplaats' listing date: 'Vandaag', 'Gisteren', 'Eergisteren' or '9 okt 26'."""
    if not text:
        return None
    t = text.strip().lower()
    if t in _RELATIVE:
        return today - timedelta(days=_RELATIVE[t])
    m = re.match(r"(\d{1,2}) ([a-z]{3})\.? ?'?(\d{2,4})$", t)
    if not m or m.group(2) not in _NL_MONTHS:
        return None
    year = int(m.group(3))
    year = year + 2000 if year < 100 else year
    try:
        return date(year, _NL_MONTHS[m.group(2)], int(m.group(1)))
    except ValueError:
        return None


def _attrs(item: dict[str, Any]) -> dict[str, str]:
    out = {a["key"]: a.get("value") for a in item.get("attributes") or [] if a.get("key")}
    for a in item.get("extendedAttributes") or []:
        out.setdefault(a["key"], a.get("value"))
    return out


def _scrub(item: dict[str, Any]) -> dict[str, Any]:
    out = scrub(item, private=True)  # seller type is unreliable: strip as if private
    for k in ("description", "categorySpecificDescription", "title"):
        if isinstance(out.get(k), str):
            out[k] = scrub_text(out[k])
    return out


class Marktplaats:
    name = "marktplaats"
    country = "NL"
    origin = ORIGIN
    headers = {"Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8", "Accept": "application/json"}

    def search(self, params: dict[str, Any], client: PoliteClient) -> list[RawListing]:
        motor = re.compile(params["motor_regex"], re.I) if params.get("motor_regex") else None
        bodies = set(params.get("bodies") or [])
        seen: dict[str, RawListing] = {}
        offset, total, max_page = 0, None, 1
        while total is None or (offset < total and offset // PAGE_SIZE < max_page):
            resp = client.get(
                build_api_url(params, offset, origin=self.origin), headers=self.headers
            )
            if resp.status_code != 200:
                raise ParseError(f"HTTP {resp.status_code} from Marktplaats API")
            try:
                data = resp.json()
                items = data["listings"]
                total = int(data["totalResultCount"])
                max_page = int(data.get("maxAllowedPageNumber") or 27)
            except (ValueError, KeyError) as e:
                raise ParseError(f"unexpected Marktplaats API response: {e}") from e
            fetched_at = datetime.now(UTC)
            for item in items:
                lid = item.get("itemId")
                if not lid or lid in seen:
                    continue
                if (item.get("priceInfo") or {}).get("priceType") not in USABLE_PRICE_TYPES:
                    continue
                attrs = _attrs(item)
                if bodies and attrs.get("body") not in bodies:
                    continue
                if motor and not motor.search(item.get("title") or ""):
                    continue
                seen[lid] = RawListing(
                    source=self.name,
                    source_listing_id=lid,
                    url=self.origin + item["vipUrl"],
                    data=_scrub(item),
                    fetched_at=fetched_at,
                )
            log.info(
                "marktplaats.search.page",
                offset=offset,
                total=total,
                items=len(items),
                kept=len(seen),
            )
            if not items:
                break
            offset += PAGE_SIZE
        return list(seen.values())

    def fetch_detail(self, raw: RawListing, client: PoliteClient) -> dict[str, Any] | None:
        return None  # comparables don't need detail pages

    def parse(self, raw: RawListing) -> NormalizedListing:
        d = raw.data
        attrs = _attrs(d)
        cents = (d.get("priceInfo") or {}).get("priceCents")
        if not cents:
            raise ParseError(f"listing {raw.source_listing_id} has no price")
        title = d.get("title") or ""
        m = re.search(r"\b(\d{3}[a-z]{1,2})\b", title, re.I)
        model = m.group(1).lower() if m else (attrs.get("model") or "").lower()
        year = attrs.get("constructionYear")
        seller = d.get("sellerInformation") or {}
        is_dealer = bool(seller.get("showWebsiteUrl") or seller.get("isVerified"))
        loc = d.get("location") or {}
        photos = [
            p.get("extraExtraLargeUrl") or p.get("largeUrl") or p.get("url")
            for p in d.get("pictures") or []
            if p.get("extraExtraLargeUrl") or p.get("largeUrl") or p.get("url")
        ]
        return NormalizedListing(
            source=self.name,
            source_listing_id=raw.source_listing_id,
            url=raw.url,
            make="BMW" if "bmw" in title.lower() else (title.split()[0] if title else ""),
            model=model,
            variant=title,
            body_type=BODY.get(attrs.get("body") or "", (attrs.get("body") or "").lower() or None),
            fuel=FUEL.get(attrs.get("fuel") or "", (attrs.get("fuel") or "").lower() or None),
            gearbox=GEARBOX.get(attrs.get("transmission") or ""),
            first_registration=date(int(year), 1, 1) if year and year.isdigit() else None,
            mileage_km=int(re.sub(r"\D", "", attrs["mileage"])) if attrs.get("mileage") else None,
            price_eur=to_eur(int(cents) / 100, "EUR"),
            seller_type=SellerType.dealer if is_dealer else SellerType.private,
            seller_name=None,
            country=self.country,
            city=loc.get("cityName"),
            postcode=None,
            photos=photos,
            description=scrub_text(d.get("description")),
            listed_at=parse_listed_date(d.get("date"), raw.fetched_at.date()),
            reserved=bool(d.get("reserved")) if d.get("reserved") is not None else None,
            raw={"search": d, "year_only": True},
        )


class Tweedehands(Marktplaats):
    """2dehands.be: the Belgian Marktplaats, same API and categories."""

    name = "tweedehands_be"
    country = "BE"
    origin = "https://www.2dehands.be"
    headers = {"Accept-Language": "nl-BE,nl;q=0.9,fr-BE;q=0.8", "Accept": "application/json"}


register(Marktplaats())
register(Tweedehands())
