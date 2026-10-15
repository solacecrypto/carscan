"""Kleinanzeigen.de via its server-rendered result HTML. See docs/sources/kleinanzeigen.md."""

import re
from datetime import UTC, date, datetime
from typing import Any

import structlog
from selectolax.lexbor import LexborHTMLParser

from app.db.models import SellerType
from app.normalize import NormalizedListing, RawListing, scrub_text, to_eur
from app.scrapers.base import register
from app.scrapers.errors import ParseError
from app.scrapers.http import PoliteClient

log = structlog.get_logger()

ORIGIN = "https://www.kleinanzeigen.de"
MAX_PAGES = 50
_PRICE_RE = re.compile(r"([\d.]+)\s*€")
_KM_RE = re.compile(r"([\d.]+)\s*km")
_EZ_RE = re.compile(r"EZ\s*(\d{2})/(\d{4})")
_LOC_RE = re.compile(r"^(\d{5})\s+(.+)$")
BODY_HINTS = (
    ("touring", "estate"),
    ("kombi", "estate"),
    ("limousine", "sedan"),
    ("coupé", "coupe"),
    ("coupe", "coupe"),
    ("cabrio", "convertible"),
)


def build_search_url(params: dict[str, Any], page: int) -> str:
    """/s-autos/bmw/[seite:N/]<query>/k0c216+autos.ez_i:2008,2014+autos.marke_s:bmw"""
    base = params.get("path", "/s-autos")
    make = params.get("make", "bmw")
    query = params.get("query", "")
    attrs = [
        f"autos.ez_i:{params.get('ez_from', '')},{params.get('ez_to', '')}",
        f"autos.marke_s:{make}",
    ]
    for extra in params.get("attributes") or []:
        attrs.append(str(extra))
    segments = [make] + ([f"seite:{page}"] if page > 1 else []) + ([query] if query else [])
    return f"{ORIGIN}{base}/{'/'.join(segments)}/k0c216+{'+'.join(attrs)}"


def parse_articles(html: str) -> list[dict[str, Any]]:
    tree = LexborHTMLParser(html)
    articles = tree.css("article[data-adid]")
    if not articles and "k0c216" not in html:
        raise ParseError("no ad articles found (markup changed or blocked)")
    out: list[dict[str, Any]] = []
    for a in articles:
        for script in a.css("script"):
            script.decompose()
        text = re.sub(r"\s+", " ", a.text())
        anchors = [n for n in a.css("a[href*='/s-anzeige/']") if n.text().strip()]
        title_node = max(anchors, key=lambda n: len(n.text().strip()), default=None)
        img = a.css_first("img")
        price_node = _smallest(a, lambda t: "€" in t and bool(_PRICE_RE.search(t)))
        loc_node = _smallest(a, lambda t: bool(_LOC_RE.fullmatch(t)) and len(t) < 60)
        out.append(
            {
                "id": a.attributes.get("data-adid"),
                "href": a.attributes.get("data-href")
                or (title_node.attributes.get("href") if title_node else None),
                "title": title_node.text().strip() if title_node else None,
                "price_text": price_node.text().strip() if price_node else None,
                "location": loc_node.text().strip() if loc_node else None,
                "image": img.attributes.get("src") if img else None,
                "text": scrub_text(text),
            }
        )
    return out


def _smallest(article: Any, pred: Any) -> Any:
    """The innermost (shortest-text) element whose stripped text satisfies `pred`."""
    best = None
    for n in article.css("p, div, span, h2"):
        t = re.sub(r"\s+", " ", n.text()).strip()
        if t and pred(t) and (best is None or len(t) < len(best[0])):
            best = (t, n)
    return best[1] if best else None


def _parse_price(text: str | None) -> tuple[int | None, bool]:
    if not text:
        return None, False
    m = _PRICE_RE.search(text)
    if not m:
        return None, False
    return int(m.group(1).replace(".", "")), "VB" in text


class Kleinanzeigen:
    name = "kleinanzeigen"
    country = "DE"
    headers = {"Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}

    def search(self, params: dict[str, Any], client: PoliteClient) -> list[RawListing]:
        motor = re.compile(params["motor_regex"], re.I) if params.get("motor_regex") else None
        seen: dict[str, RawListing] = {}
        page = 1
        while page <= min(int(params.get("max_pages") or MAX_PAGES), MAX_PAGES):
            resp = client.get(build_search_url(params, page), headers=self.headers)
            if resp.status_code != 200:
                raise ParseError(f"HTTP {resp.status_code} on Kleinanzeigen page {page}")
            items = parse_articles(resp.text)
            fetched_at = datetime.now(UTC)
            new = 0
            for item in items:
                lid = item.get("id")
                if not lid or lid in seen or not item.get("href"):
                    continue
                if motor and not motor.search(item.get("title") or ""):
                    continue
                price, _ = _parse_price(item.get("price_text"))
                if price is None:
                    continue  # "VB" without amount, "Zu verschenken", etc.
                seen[lid] = RawListing(
                    source=self.name,
                    source_listing_id=lid,
                    url=ORIGIN + item["href"],
                    data=item,
                    fetched_at=fetched_at,
                )
                new += 1
            log.info("kleinanzeigen.search.page", page=page, items=len(items), kept=len(seen))
            if not items or f"seite:{page + 1}/" not in resp.text:
                break
            page += 1
        return list(seen.values())

    def fetch_detail(self, raw: RawListing, client: PoliteClient) -> dict[str, Any] | None:
        return None

    def parse(self, raw: RawListing) -> NormalizedListing:
        d = raw.data
        price, negotiable = _parse_price(d.get("price_text"))
        if price is None:
            raise ParseError(f"listing {raw.source_listing_id} has no price")
        title = d.get("title") or ""
        text = d.get("text") or ""
        m = re.search(r"\b(\d{3}[a-z]{1,2})\b", title, re.I)
        km = _KM_RE.search(text)
        ez = _EZ_RE.search(text)
        loc = _LOC_RE.match(d.get("location") or "")
        lower = title.lower()
        body = next((b for hint, b in BODY_HINTS if hint in lower), None)
        gearbox = "automatic" if "automatik" in lower else ("manual" if "schalt" in lower else None)
        return NormalizedListing(
            source=self.name,
            source_listing_id=raw.source_listing_id,
            url=raw.url,
            make="BMW" if "bmw" in lower else (title.split()[0] if title else ""),
            model=m.group(1).lower() if m else "",
            variant=title,
            body_type=body,
            fuel="petrol" if m and m.group(1).lower().endswith("i") else None,
            gearbox=gearbox,
            first_registration=date(int(ez.group(2)), int(ez.group(1)), 1) if ez else None,
            mileage_km=int(km.group(1).replace(".", "")) if km else None,
            price_eur=to_eur(price, "EUR"),
            seller_type=SellerType.private,  # not exposed on the list page; most ads are private
            country="DE",
            city=loc.group(2) if loc else None,
            postcode=loc.group(1) if loc else None,
            photos=[d["image"]] if d.get("image") else [],
            description=None,
            raw={"search": d, "negotiable": negotiable},
        )


register(Kleinanzeigen())
