"""Subito.it via the Next.js state embedded in the results page. See docs/sources/subito.md."""

import re
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlencode

import structlog

from app.db.models import SellerType
from app.normalize import NormalizedListing, RawListing, scrub, scrub_text, to_eur
from app.scrapers.autoscout24_de import extract_next_data
from app.scrapers.base import register
from app.scrapers.errors import ParseError
from app.scrapers.http import PoliteClient

log = structlog.get_logger()

ORIGIN = "https://www.subito.it"
MAX_PAGES = 30
BODY = {
    "Berlina": "sedan",
    "Station Wagon": "estate",
    "Coupé": "coupe",
    "Cabrio": "convertible",
    "SUV/Fuoristrada": "suv",
    "Monovolume": "van",
    "Citycar": "hatchback",
}
GEARBOX = {"Manuale": "manual", "Automatico": "automatic", "Semiautomatico": "semi-automatic"}
FUEL = {
    "Benzina": "petrol",
    "Diesel": "diesel",
    "GPL": "lpg",
    "Metano": "cng",
    "Elettrica": "electric",
    "Ibrida": "hybrid",
}


def build_search_url(params: dict[str, Any], page: int) -> str:
    query = {"q": params.get("q", ""), "ys": params.get("ys", ""), "ye": params.get("ye", "")}
    query = {k: v for k, v in query.items() if v != ""}
    if page > 1:
        query["o"] = page
    return f"{ORIGIN}{params.get('path', '/annunci-italia/vendita/auto/')}?{urlencode(query)}"


def _feature(ad: dict[str, Any], uri: str) -> str | None:
    f = (ad.get("features") or {}).get(uri) or {}
    vals = f.get("values") or []
    return vals[0].get("value") if vals else None


def _scrub(ad: dict[str, Any]) -> dict[str, Any]:
    adv = dict(ad.get("advertiser") or {})
    private = not adv.get("company")
    out = scrub(ad, private=private)
    out["advertiser"] = {
        k: v
        for k, v in adv.items()
        if k in ("type", "company", "shopId") or (k == "shopName" and not private)
    }
    out["body"] = scrub_text(ad.get("body") or "")
    return out


class Subito:
    name = "subito"
    country = "IT"
    headers = {"Accept-Language": "it-IT,it;q=0.9,en;q=0.8"}

    def search(self, params: dict[str, Any], client: PoliteClient) -> list[RawListing]:
        motor = re.compile(params["motor_regex"], re.I) if params.get("motor_regex") else None
        bodies = set(params.get("bodies") or [])
        seen: dict[str, RawListing] = {}
        page, total_pages = 1, 1
        while page <= min(total_pages, MAX_PAGES):
            resp = client.get(build_search_url(params, page), headers=self.headers)
            if resp.status_code != 200:
                raise ParseError(f"HTTP {resp.status_code} on Subito page {page}")
            pp = extract_next_data(resp.text)
            try:
                items = pp["initialState"]["items"]
                ads = items["originalList"]
                total_pages = int(items.get("totalPages") or 1)
            except (KeyError, TypeError) as e:
                raise ParseError(f"Subito state has unexpected shape: {e}") from e
            fetched_at = datetime.now(UTC)
            for ad in ads:
                lid = (ad.get("urn") or "").rsplit(":", 1)[-1]
                if not lid or lid in seen:
                    continue
                if bodies and _feature(ad, "/car_type") not in bodies:
                    continue
                if motor and not motor.search(ad.get("subject") or ""):
                    continue
                url = (ad.get("urls") or {}).get("default")
                if not url or not _feature(ad, "/price"):
                    continue
                seen[lid] = RawListing(
                    source=self.name,
                    source_listing_id=lid,
                    url=url,
                    data=_scrub(ad),
                    fetched_at=fetched_at,
                )
            log.info(
                "subito.search.page",
                page=page,
                total_pages=total_pages,
                items=len(ads),
                kept=len(seen),
            )
            if not ads:
                break
            page += 1
        return list(seen.values())

    def fetch_detail(self, raw: RawListing, client: PoliteClient) -> dict[str, Any] | None:
        return None

    def parse(self, raw: RawListing) -> NormalizedListing:
        d = raw.data
        price_txt = _feature(d, "/price") or ""
        digits = re.sub(r"\D", "", price_txt)
        if not digits:
            raise ParseError(f"listing {raw.source_listing_id} has no price")
        subject = d.get("subject") or ""
        m = re.search(r"\b(\d{3}[a-z]{1,2})\b", subject, re.I)
        reg = _feature(d, "/register_date")  # "04/2008"
        rm = re.fullmatch(r"(\d{2})/(\d{4})", reg or "")
        year = _feature(d, "/year")
        km_txt = _feature(d, "/mileage_scalar") or ""
        kw = re.search(r"(\d+)\s*kW", _feature(d, "/power") or "")
        geo = d.get("geo") or {}
        town = (geo.get("town") or {}).get("value") or (geo.get("city") or {}).get("value")
        adv = d.get("advertiser") or {}
        body_it = _feature(d, "/car_type") or ""
        fuel_it = _feature(d, "/fuel") or ""
        gear_it = _feature(d, "/gearbox") or ""
        images = d.get("images") or []
        photos = [f"{i['cdnBaseUrl']}?rule=fullscreen-1280" for i in images if i.get("cdnBaseUrl")][
            :20
        ]
        return NormalizedListing(
            source=self.name,
            source_listing_id=raw.source_listing_id,
            url=raw.url,
            make=_feature(d, "/car") or ("BMW" if "bmw" in subject.lower() else ""),
            model=m.group(1).lower() if m else "",
            variant=subject,
            body_type=BODY.get(body_it, body_it.lower() or None),
            fuel=FUEL.get(fuel_it, fuel_it.lower() or None),
            gearbox=GEARBOX.get(gear_it),
            power_kw=int(kw.group(1)) if kw else None,
            first_registration=date(int(rm.group(2)), int(rm.group(1)), 1)
            if rm
            else (date(int(year), 1, 1) if year and year.isdigit() else None),
            mileage_km=int(re.sub(r"\D", "", km_txt)) if re.sub(r"\D", "", km_txt) else None,
            price_eur=to_eur(int(digits), "EUR"),
            seller_type=SellerType.dealer if adv.get("company") else SellerType.private,
            seller_name=adv.get("shopName") if adv.get("company") else None,
            country="IT",
            city=town,
            photos=photos,
            description=d.get("body") or None,
            raw={"search": d, "year_only": not rm},
        )


register(Subito())
