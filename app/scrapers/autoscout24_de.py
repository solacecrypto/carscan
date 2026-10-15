"""AutoScout24 (.de for deals, .nl for comparables). See docs/sources/autoscout24_*.md."""

import json
import re
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlencode

import structlog

from app.db.models import SellerType
from app.normalize import NormalizedListing, RawListing, scrub, scrub_text, to_eur
from app.scrapers.base import register
from app.scrapers.errors import ParseError
from app.scrapers.http import PoliteClient

log = structlog.get_logger()

ORIGIN = "https://www.autoscout24.de"
MAX_PAGES = 20  # AutoScout24 never serves more than 20 pages per query
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_KW_RE = re.compile(r"(\d+)\s*kW")
_INT_RE = re.compile(r"\d+")

# German, Dutch, Italian and French labels (the .de/.nl/.be/.it sites localise these)
GEARBOX = {
    "Schaltgetriebe": "manual",
    "Automatik": "automatic",
    "Halbautomatik": "semi-automatic",
    "Handgeschakeld": "manual",
    "Automaat": "automatic",
    "Semi-automaat": "semi-automatic",
    "Manuale": "manual",
    "Automatico": "automatic",
    "Semiautomatico": "semi-automatic",
    "Manuelle": "manual",
    "Automatique": "automatic",
    "Manueel": "manual",
    "Automatisch": "automatic",
    "Halfautomaat": "semi-automatic",
}
FUEL = {
    "Benzin": "petrol",
    "Benzine": "petrol",
    "Benzina": "petrol",
    "Essence": "petrol",
    "Diesel": "diesel",
    "Elektro": "electric",
    "Elektrisch": "electric",
    "Elettrica": "electric",
    "Électrique": "electric",
    "Elektro/Benzin": "hybrid-petrol",
    "Elektro/Diesel": "hybrid-diesel",
    "Autogas (LPG)": "lpg",
    "LPG": "lpg",
    "GPL": "lpg",
    "Erdgas (CNG)": "cng",
    "CNG": "cng",
    "Metano": "cng",
}
BODY = {
    "Limousine": "sedan",
    "Sedan": "sedan",
    "Berlina": "sedan",
    "Kombi": "estate",
    "Stationwagen": "estate",
    "Station Wagon": "estate",
    "Break": "estate",
    "Touring": "estate",
    "Coupé": "coupe",
    "Cabrio": "convertible",
    "SUV/Geländewagen/Pickup": "suv",
    "Kleinwagen": "hatchback",
    "Van/Kleinbus": "van",
    "Gran Turismo": "hatchback",
}


CLIENT_SIDE_PARAMS = ("path", "models", "motor_regex")


def build_search_url(params: dict[str, Any], page: int, origin: str = ORIGIN) -> str:
    """`params` mirrors the AutoScout24 query string (see docs). `path` picks the list page."""
    path = params.get("path", "/lst")
    query = {k: v for k, v in params.items() if k not in CLIENT_SIDE_PARAMS}
    query.setdefault("atype", "C")
    query.setdefault("damaged_listing", "exclude")
    query.setdefault("sort", "age")
    query.setdefault("desc", "1")
    query["page"] = page
    return f"{origin}{path}?{urlencode(query, safe=',|')}"


def extract_next_data(html: str) -> dict[str, Any]:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise ParseError("__NEXT_DATA__ not found (markup changed or challenge page)")
    try:
        return json.loads(m.group(1))["props"]["pageProps"]
    except (ValueError, KeyError) as e:
        raise ParseError(f"__NEXT_DATA__ has unexpected shape: {e}") from e


def _is_private(d: dict[str, Any]) -> bool:
    return (d.get("seller") or {}).get("type") == "PrivateSeller"


def _scrub_listing(d: dict[str, Any]) -> dict[str, Any]:
    out = scrub(d, private=_is_private(d))
    if isinstance(out.get("description"), str):
        out["description"] = scrub_text(out["description"])
    return out


def _parse_int(s: str | None) -> int | None:
    if not s:
        return None
    digits = "".join(_INT_RE.findall(s))
    return int(digits) if digits else None


def _parse_first_registration(s: str | None) -> date | None:
    """Accepts '08-2008', '08/2008' or '2011-08-01'."""
    if not s:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return date.fromisoformat(s)
    m = re.fullmatch(r"(\d{2})[-/](\d{4})", s)
    if m:
        return date(int(m.group(2)), int(m.group(1)), 1)
    return None


def _full_size(url: str) -> str:
    return re.sub(r"/\d+x\d+\.webp$", "/1280x960.webp", url)


class AutoScout24DE:
    name = "autoscout24_de"
    country = "DE"
    origin = ORIGIN
    headers = {"Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}

    # ---- search -----------------------------------------------------------------------
    def search(self, params: dict[str, Any], client: PoliteClient) -> list[RawListing]:
        seen: dict[str, RawListing] = {}
        models = {str(m).lower() for m in params.get("models") or []}
        motor = re.compile(params["motor_regex"], re.I) if params.get("motor_regex") else None
        page, total_pages = 1, 1
        while page <= min(total_pages, MAX_PAGES):
            url = build_search_url(params, page, origin=self.origin)
            resp = client.get(url, headers=self.headers)
            if resp.status_code != 200:
                raise ParseError(f"HTTP {resp.status_code} on search page {page}")
            pp = extract_next_data(resp.text)
            listings = pp.get("listings")
            if listings is None:
                raise ParseError("pageProps.listings missing on search page")
            total_pages = int(pp.get("numberOfPages") or 1)
            fetched_at = datetime.now(UTC)
            for item in listings:
                lid = item.get("id")
                if not lid or lid in seen:
                    continue
                veh = item.get("vehicle") or {}
                if models and str(veh.get("model") or "").lower() not in models:
                    continue
                if motor and not motor.search(veh.get("modelVersionInput") or ""):
                    continue
                seen[lid] = RawListing(
                    source=self.name,
                    source_listing_id=lid,
                    url=self.origin + item["url"],
                    data=_scrub_listing(item),
                    fetched_at=fetched_at,
                )
            log.info("as24.search.page", page=page, total_pages=total_pages, items=len(listings))
            if not listings:
                break
            page += 1
        if total_pages > MAX_PAGES:
            log.warning("as24.search.truncated", total_pages=total_pages, max_pages=MAX_PAGES)
        return list(seen.values())

    # ---- detail -----------------------------------------------------------------------
    def fetch_detail(self, raw: RawListing, client: PoliteClient) -> dict[str, Any] | None:
        resp = client.get(raw.url, headers=self.headers)
        if resp.status_code in (301, 302, 404, 410):
            return None  # listing gone: AS24 redirects to the list page
        if resp.status_code != 200:
            raise ParseError(f"HTTP {resp.status_code} on detail page")
        pp = extract_next_data(resp.text)
        details = pp.get("listingDetails")
        if not details:
            raise ParseError("pageProps.listingDetails missing on detail page")
        return _scrub_listing(details)

    # ---- parse ------------------------------------------------------------------------
    def parse(self, raw: RawListing) -> NormalizedListing:
        d = raw.data
        det = raw.detail or {}
        veh = d.get("vehicle") or {}
        dveh = det.get("vehicle") or {}
        details = {x.get("ariaLabel"): x.get("data") for x in d.get("vehicleDetails") or []}
        tracking = d.get("tracking") or {}
        # search-page price wins: it is re-fetched every run, the detail page only once
        pub = (det.get("prices") or {}).get("public") or {}
        price = (d.get("price") or {}).get("priceRaw") or pub.get("priceRaw")
        if price is None:
            raise ParseError(f"listing {raw.source_listing_id} has no price")

        seller = d.get("seller") or det.get("seller") or {}
        private = seller.get("type") == "PrivateSeller"
        gearbox_src = (
            dveh.get("transmissionType") or veh.get("transmission") or details.get("Getriebe")
        )
        fuel_src = veh.get("fuel") or details.get("Kraftstoff")
        body_src = dveh.get("bodyType") or veh.get("variant")
        co2 = (dveh.get("co2emissionInGramPerKmWithFallback") or {}).get("raw") or (
            (dveh.get("wltp") or {}).get("co2EmissionsCombinedWithFallback") or {}
        ).get("raw")
        loc = d.get("location") or det.get("location") or {}
        photos = [_full_size(u) for u in (det.get("images") or d.get("images") or [])]
        description = det.get("description")
        if description:
            description = re.sub(r"<br\s*/?>", "\n", description)
            description = re.sub(r"<[^>]+>", "", description).strip()

        leads = (d.get("statistics") or {}).get("leadsRange")
        return NormalizedListing(
            source=self.name,
            source_listing_id=raw.source_listing_id,
            url=raw.url,
            demand=str(leads) if leads else None,
            make=veh.get("make") or dveh.get("make") or "",
            model=_model_name(veh, dveh),
            variant=veh.get("modelVersionInput") or dveh.get("modelVersionInput"),
            body_type=BODY.get(body_src, body_src.lower() if body_src else None),
            fuel=FUEL.get(fuel_src, fuel_src.lower() if fuel_src else None),
            gearbox=GEARBOX.get(gearbox_src, gearbox_src.lower() if gearbox_src else None),
            power_kw=dveh.get("rawPowerInKw") or _kw(details.get("Leistung")),
            first_registration=_parse_first_registration(
                dveh.get("firstRegistrationDateRaw") or tracking.get("firstRegistration")
            ),
            mileage_km=dveh.get("mileageInKmRaw")
            or _parse_int(tracking.get("mileage"))
            or _parse_int(veh.get("mileageInKm")),
            price_eur=to_eur(price, "EUR"),
            vat_deductible=pub.get("taxDeductible") if pub else None,
            co2_g_km=int(co2) if co2 else None,
            vin=None,  # not exposed by AutoScout24
            seller_type=SellerType.private if private else SellerType.dealer,
            seller_name=None if private else seller.get("companyName"),
            country=(loc.get("countryCode") or "DE").upper()[:2],
            city=loc.get("city"),
            postcode=loc.get("zip"),
            photos=photos,
            description=scrub_text(description),
            raw={"search": d, "detail": det} if det else {"search": d},
        )


def _model_name(veh: dict[str, Any], dveh: dict[str, Any]) -> str:
    """'320i' from motorTypeName; on .it that is null, so derive it from the version string."""
    name = veh.get("motorTypeName") or dveh.get("motorTypeName")
    if name:
        return str(name)
    m = re.search(r"\b(\d{3}[a-z]{1,2})\b", veh.get("modelVersionInput") or "", re.I)
    if m:
        return m.group(1).lower()
    model = str(veh.get("model") or dveh.get("model") or "")
    fuel = (veh.get("fuel") or "").lower()
    if model.isdigit() and fuel in ("benzin", "benzina", "benzine", "essence", "petrol"):
        return model + "i"
    return model


def _kw(s: str | None) -> int | None:
    m = _KW_RE.search(s or "")
    return int(m.group(1)) if m else None


class AutoScout24NL(AutoScout24DE):
    name = "autoscout24_nl"
    country = "NL"
    origin = "https://www.autoscout24.nl"
    headers = {"Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8"}


class AutoScout24BE(AutoScout24DE):
    name = "autoscout24_be"
    country = "BE"
    origin = "https://www.autoscout24.be"
    headers = {"Accept-Language": "nl-BE,nl;q=0.9,fr-BE;q=0.8"}


class AutoScout24IT(AutoScout24DE):
    name = "autoscout24_it"
    country = "IT"
    origin = "https://www.autoscout24.it"
    headers = {"Accept-Language": "it-IT,it;q=0.9,en;q=0.8"}


register(AutoScout24DE())
register(AutoScout24NL())
register(AutoScout24BE())
register(AutoScout24IT())
