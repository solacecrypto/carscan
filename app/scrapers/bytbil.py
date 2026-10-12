"""Bytbil.com via its server-rendered result HTML. Prices in SEK. See docs/sources/bytbil.md."""

import re
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlencode

import structlog
from selectolax.lexbor import LexborHTMLParser

from app.db.models import SellerType
from app.normalize import NormalizedListing, RawListing, scrub_text, to_eur
from app.scrapers.base import register
from app.scrapers.errors import ParseError
from app.scrapers.http import PoliteClient

log = structlog.get_logger()

ORIGIN = "https://www.bytbil.com"
MAX_PAGES = 20
_META_RE = re.compile(r"(\d{4})\s*\|\s*([\d\s]+)\s*mil\s*\|\s*(.+)")
_PRICE_RE = re.compile(r"([\d\s]+)\s*kr")
BODY_HINTS = (
    ("touring", "estate"),
    ("kombi", "estate"),
    ("sedan", "sedan"),
    ("coupé", "coupe"),
    ("coupe", "coupe"),
    ("cabriolet", "convertible"),
    ("convertible", "convertible"),
)


def build_search_url(params: dict[str, Any], page: int) -> str:
    query = {k: v for k, v in params.items() if k not in ("motor_regex", "path")}
    if page > 1:
        query["Page"] = page
    return f"{ORIGIN}{params.get('path', '/bil')}?{urlencode(query)}"


def parse_items(html: str) -> list[dict[str, Any]]:
    tree = LexborHTMLParser(html)
    nodes = tree.css("li.result-list-item")
    if not nodes and "result-list" not in html:
        raise ParseError("no result list found (markup changed or blocked)")
    out: list[dict[str, Any]] = []
    for n in nodes:
        a = n.css_first("a[href*='/']")
        h3 = n.css_first("h3")
        meta = next((p.text().strip() for p in n.css("p") if "|" in p.text()), None)
        price = next(
            (
                re.sub(r"\s+", " ", x.text()).strip()
                for x in n.css("div, span, p")
                if "kr" in x.text() and _PRICE_RE.search(x.text()) and len(x.text()) < 60
            ),
            None,
        )
        href = a.attributes.get("href") if a else None
        lid = href.rsplit("-", 1)[-1] if href else None
        out.append(
            {
                "id": lid,
                "href": href,
                "title": h3.text().strip() if h3 else None,
                "meta": meta,
                "price_text": price,
                "text": scrub_text(re.sub(r"\s+", " ", n.text())),
            }
        )
    return out


class Bytbil:
    name = "bytbil"
    country = "SE"
    headers = {"Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8"}

    def search(self, params: dict[str, Any], client: PoliteClient) -> list[RawListing]:
        motor = re.compile(params["motor_regex"], re.I) if params.get("motor_regex") else None
        seen: dict[str, RawListing] = {}
        page = 1
        while page <= MAX_PAGES:
            resp = client.get(build_search_url(params, page), headers=self.headers)
            if resp.status_code != 200:
                raise ParseError(f"HTTP {resp.status_code} on Bytbil page {page}")
            items = parse_items(resp.text)
            fetched_at = datetime.now(UTC)
            for item in items:
                lid = item.get("id")
                if not lid or lid in seen or not item.get("href") or not item.get("price_text"):
                    continue
                if motor and not motor.search(item.get("title") or ""):
                    continue
                seen[lid] = RawListing(
                    source=self.name,
                    source_listing_id=lid,
                    url=ORIGIN + item["href"],
                    data=item,
                    fetched_at=fetched_at,
                )
            log.info("bytbil.search.page", page=page, items=len(items), kept=len(seen))
            if not items or f"Page={page + 1}" not in resp.text:
                break
            page += 1
        return list(seen.values())

    def fetch_detail(self, raw: RawListing, client: PoliteClient) -> dict[str, Any] | None:
        return None

    def parse(self, raw: RawListing) -> NormalizedListing:
        d = raw.data
        pm = _PRICE_RE.search(d.get("price_text") or "")
        if not pm:
            raise ParseError(f"listing {raw.source_listing_id} has no price")
        sek = int(re.sub(r"\D", "", pm.group(1)))
        title = d.get("title") or ""
        mm = _META_RE.search(d.get("meta") or "")
        m = re.search(r"\b(\d{3})\s?([a-z]{1,2})\b", title, re.I)  # "320 i" or "320i"
        lower = title.lower()
        return NormalizedListing(
            source=self.name,
            source_listing_id=raw.source_listing_id,
            url=raw.url,
            make="BMW" if "bmw" in lower else (title.split()[0] if title else ""),
            model=(m.group(1) + m.group(2)).lower() if m else "",
            variant=title,
            body_type=next((b for hint, b in BODY_HINTS if hint in lower), None),
            fuel="petrol" if m and m.group(2).lower() == "i" else None,
            gearbox="automatic"
            if "automat" in lower
            else ("manual" if "manuell" in lower else None),
            first_registration=date(int(mm.group(1)), 1, 1) if mm else None,
            mileage_km=int(re.sub(r"\D", "", mm.group(2))) * 10 if mm else None,
            price_eur=to_eur(sek, "SEK"),
            seller_type=SellerType.dealer,  # Bytbil lists dealer stock
            country="SE",
            city=mm.group(3).strip().title() if mm else None,
            photos=[],
            raw={"search": d, "price_sek": sek, "year_only": True},
        )


register(Bytbil())
