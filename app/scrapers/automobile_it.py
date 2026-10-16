"""Automobile.it via the Next.js data on the results page. See docs/sources/automobile_it.md."""

import re
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlencode

import structlog

from app.db.models import SellerType
from app.normalize import NormalizedListing, RawListing, scrub_text, to_eur
from app.normalize.attrs import canonical_color
from app.scrapers.autoscout24_de import extract_next_data
from app.scrapers.base import register
from app.scrapers.errors import ParseError
from app.scrapers.http import PoliteClient

log = structlog.get_logger()

ORIGIN = "https://www.automobile.it"
MAX_PAGES = 100
MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "gennaio",
            "febbraio",
            "marzo",
            "aprile",
            "maggio",
            "giugno",
            "luglio",
            "agosto",
            "settembre",
            "ottobre",
            "novembre",
            "dicembre",
        ],
        1,
    )
}
GEARBOX = {"Manuale": "manual", "Automatico": "automatic", "Semiautomatico": "semi-automatic"}
BODY_HINTS = (
    ("touring", "estate"),
    ("station wagon", "estate"),
    ("sw", "estate"),
    ("coupé", "coupe"),
    ("coupe", "coupe"),
    ("cabrio", "convertible"),
    ("berlina", "sedan"),
)


def build_search_url(params: dict[str, Any], page: int) -> str:
    query = {k: params[k] for k in ("attr_m_year_min", "attr_m_year_max") if params.get(k)}
    query["p"] = page
    return f"{ORIGIN}{params['path']}?{urlencode(query)}"


class AutomobileIt:
    name = "automobile_it"
    country = "IT"
    headers = {"Accept-Language": "it-IT,it;q=0.9,en;q=0.8"}

    def search(self, params: dict[str, Any], client: PoliteClient) -> list[RawListing]:
        motor = re.compile(params["motor_regex"], re.I) if params.get("motor_regex") else None
        fuel = (
            params.get("fuel") or ""
        ).lower()  # "benzina": matched against details.fuelEmissions
        seen: dict[str, RawListing] = {}
        page, total_pages = 1, 1
        limit = min(int(params.get("max_pages") or MAX_PAGES), MAX_PAGES)
        while page <= min(total_pages, limit):
            resp = client.get(build_search_url(params, page), headers=self.headers)
            if resp.status_code != 200:
                raise ParseError(f"HTTP {resp.status_code} on automobile.it page {page}")
            pp = extract_next_data(resp.text)
            try:
                res = pp["apiResults"]["result"]
                items = res["resultList"]
                total_pages = int(res["page"]["totalPages"])
            except (KeyError, TypeError) as e:
                raise ParseError(f"automobile.it data has unexpected shape: {e}") from e
            fetched_at = datetime.now(UTC)
            for item in items:
                lid = str(item.get("id") or "")
                if not lid or lid in seen or not item.get("url"):
                    continue
                det = item.get("details") or {}
                if fuel and not (det.get("fuelEmissions") or "").lower().startswith(fuel):
                    continue
                if motor and not motor.search(item.get("title") or ""):
                    continue
                item = {
                    k: v
                    for k, v in item.items()
                    if k not in ("vipPictures", "extendedResult", "rentalPackages")
                }
                item["shortDescription"] = scrub_text(item.get("shortDescription"))
                seen[lid] = RawListing(
                    source=self.name,
                    source_listing_id=lid,
                    url=ORIGIN + item["url"],
                    data=item,
                    fetched_at=fetched_at,
                )
            log.info(
                "automobile_it.search.page",
                page=page,
                total_pages=total_pages,
                items=len(items),
                kept=len(seen),
            )
            if not items:
                break
            page += 1
        if total_pages > limit:
            log.warning("automobile_it.search.truncated", total_pages=total_pages, limit=limit)
        return list(seen.values())

    def fetch_detail(self, raw: RawListing, client: PoliteClient) -> dict[str, Any] | None:
        resp = client.get(raw.url, headers=self.headers)
        if resp.status_code in (301, 302, 404, 410):
            return None
        if resp.status_code != 200:
            raise ParseError(f"HTTP {resp.status_code} on automobile.it ad page")
        pp = extract_next_data(resp.text)
        res = pp.get("result")
        if not res:
            raise ParseError("ad page without result block")
        info = {
            item.get("title"): ", ".join(item.get("values") or [])
            for group in (pp.get("vehicleInformation") or {}).values()
            if isinstance(group, list)
            for item in group
            if item.get("title")
        }
        return {"description": scrub_text(res.get("description")), "info": info}

    def parse(self, raw: RawListing) -> NormalizedListing:
        d = raw.data
        det = d.get("details") or {}
        digits = re.sub(r"\D", "", d.get("formattedPrice") or "")
        if not digits:
            raise ParseError(f"listing {raw.source_listing_id} has no price")
        title = d.get("title") or ""
        m = re.search(r"\b(\d{3}[a-z]{1,2})\b", title, re.I)
        reg = re.fullmatch(r"([A-Za-zà]+)\s+(\d{4})", (det.get("registration") or "").strip())
        month = MONTHS.get(reg.group(1).lower()) if reg else None
        km = re.sub(r"\D", "", det.get("formattedKm") or "")
        kw = re.search(r"\((\d+)\s*KW\)", det.get("formattedPower") or "", re.I)
        loc = re.fullmatch(r"(.+?)\s*\((\w+)\)", d.get("location") or "")
        lower = title.lower()
        fuel_txt = (det.get("fuelEmissions") or "").split("-")[0].strip().lower()
        body = next((b for hint, b in BODY_HINTS if hint in lower), None)
        images = (
            [(p.get("sizes") or {}).get("XL", {}).get("href") for p in d.get("pictures") or []]
            or d.get("imageUrls")
            or []
        )
        return NormalizedListing(
            source=self.name,
            source_listing_id=raw.source_listing_id,
            url=raw.url,
            make="BMW" if "bmw" in lower else (title.split()[0] if title else ""),
            model=m.group(1).lower() if m else "",
            variant=title,
            body_type=body,
            color=canonical_color(((raw.detail or {}).get("info") or {}).get("Colore esterno")),
            fuel={
                "benzina": "petrol",
                "diesel": "diesel",
                "gpl": "lpg",
                "metano": "cng",
                "ibrido": "hybrid",
                "elettrica": "electric",
            }.get(fuel_txt, fuel_txt or None),
            gearbox=GEARBOX.get(det.get("shift") or ""),
            power_kw=int(kw.group(1)) if kw else None,
            first_registration=date(int(reg.group(2)), month or 1, 1) if reg else None,
            mileage_km=int(km) if km else None,
            price_eur=to_eur(int(digits), "EUR"),
            seller_type=SellerType.dealer
            if (d.get("sellerType") or "").upper() == "DEALER"
            else SellerType.private,
            seller_name=d.get("dealerName")
            if (d.get("sellerType") or "").upper() == "DEALER"
            else None,
            country="IT",
            city=loc.group(1) if loc else d.get("location"),
            photos=[u for u in images if u][:20],
            description=(raw.detail or {}).get("description") or d.get("shortDescription") or None,
            raw={"search": d, "detail": raw.detail, "year_only": month is None}
            if raw.detail
            else {"search": d, "year_only": month is None},
        )


register(AutomobileIt())
