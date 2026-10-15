"""Machine translation of seller descriptions into English.

Providers: ``google`` (the public translate endpoint, no key, default), ``deepl`` (needs
``DEEPL_API_KEY``), ``off``. Translations are stored on ``listings.description_en`` by a
small worker job; the original text is kept and shown alongside.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.db.models import Listing, ListingInsight

log = structlog.get_logger()

MAX_CHUNK = 4000  # characters per request; ads are rarely longer than this
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)


class TranslateError(Exception):
    pass


class Translator(Protocol):
    name: str

    def translate(self, text: str) -> str:
        """Return the English text (unchanged when it already is English)."""
        ...


def chunks(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """Split on paragraph, then sentence boundaries so no request exceeds ``limit``."""
    out: list[str] = []
    buf = ""
    for para in re.split(r"(?<=\n)", text):
        if len(buf) + len(para) <= limit:
            buf += para
            continue
        if buf:
            out.append(buf)
            buf = ""
        if len(para) <= limit:
            buf = para
            continue
        for sent in re.split(r"(?<=[.!?])\s+", para):
            while len(sent) > limit:
                out.append(sent[:limit])
                sent = sent[limit:]
            if len(buf) + len(sent) + 1 > limit:
                out.append(buf)
                buf = sent
            else:
                buf = f"{buf} {sent}" if buf else sent
    if buf:
        out.append(buf)
    return out


class GoogleTranslator:
    """The endpoint behind the Google Translate web page. No key, fine for a few hundred
    short texts an hour; blocked bursts come back as 429 and raise TranslateError."""

    name = "google"
    url = "https://translate.googleapis.com/translate_a/single"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=30, headers={"User-Agent": _UA})

    def translate(self, text: str) -> str:
        parts = []
        for chunk in chunks(text):
            r = self._client.get(
                self.url,
                params={"client": "gtx", "sl": "auto", "tl": "en", "dt": "t", "q": chunk},
            )
            if r.status_code != 200:
                raise TranslateError(f"google translate HTTP {r.status_code}")
            try:
                data = r.json()
                parts.append("".join(seg[0] for seg in data[0] if seg and seg[0]))
            except (ValueError, IndexError, TypeError) as e:
                raise TranslateError(f"unexpected response: {e}") from e
        return "".join(parts)


class DeepLTranslator:
    name = "deepl"

    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        host = "api-free.deepl.com" if api_key.endswith(":fx") else "api.deepl.com"
        self.url = f"https://{host}/v2/translate"
        self._client = client or httpx.Client(
            timeout=30, headers={"Authorization": f"DeepL-Auth-Key {api_key}"}
        )

    def translate(self, text: str) -> str:
        parts = []
        for chunk in chunks(text):
            r = self._client.post(self.url, json={"text": [chunk], "target_lang": "EN"})
            if r.status_code != 200:
                raise TranslateError(f"deepl HTTP {r.status_code}")
            parts.append(r.json()["translations"][0]["text"])
        return "".join(parts)


def default_translator() -> Translator | None:
    from app.config import get_settings

    s = get_settings()
    if s.translate_provider == "deepl" and s.deepl_api_key.get_secret_value():
        return DeepLTranslator(s.deepl_api_key.get_secret_value())
    if s.translate_provider == "google":
        return GoogleTranslator()
    return None


def pending_listings(session: Session, limit: int) -> list[Listing]:
    """Active cars with a description but no English text yet, best spread first."""
    stmt = (
        select(Listing)
        .where(
            Listing.is_active.is_(True),
            Listing.duplicate_of_id.is_(None),
            Listing.description.isnot(None),
            Listing.description != "",
            Listing.description_en.is_(None),
        )
        .order_by(Listing.spread_eur.desc().nulls_last(), Listing.first_seen.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def tags_needing_evidence(tags: list[dict] | None) -> list[dict]:
    return [t for t in (tags or []) if t.get("evidence") and not t.get("evidence_en")]


def pending_evidence(session: Session, limit: int) -> list[Listing]:
    """Active cars whose condition tags still have untranslated evidence sentences."""
    stmt = (
        select(Listing)
        .join(ListingInsight)
        .where(Listing.is_active.is_(True), Listing.duplicate_of_id.is_(None))
        .where(ListingInsight.tags.isnot(None))
        .order_by(Listing.spread_eur.desc().nulls_last(), Listing.first_seen.desc())
        .limit(limit * 20)
    )
    out = []
    for row in session.scalars(stmt):
        if row.insight is not None and tags_needing_evidence(row.insight.tags):
            out.append(row)
            if len(out) >= limit:
                break
    return out


def translate_evidence(row: Listing, translator: Translator) -> int:
    """One request per car: the evidence sentences joined by newlines, split back after."""
    ins = row.insight
    if ins is None:
        return 0
    todo = tags_needing_evidence(ins.tags)
    if not todo:
        return 0
    lines = translator.translate("\n".join(t["evidence"] for t in todo)).split("\n")
    if len(lines) != len(todo):  # the provider merged or split lines; translate one by one
        lines = [translator.translate(t["evidence"]) for t in todo]
    tags = [dict(t) for t in ins.tags]
    by_ev = dict(zip((t["evidence"] for t in todo), lines, strict=True))
    for t in tags:
        if t.get("evidence") in by_ev and not t.get("evidence_en"):
            t["evidence_en"] = by_ev[t["evidence"]].strip()
    ins.tags = tags
    flag_modified(ins, "tags")
    return len(todo)


def translate_pending(
    session: Session,
    translator: Translator,
    now: datetime,
    limit: int = 50,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    """Translate up to ``limit`` descriptions with polite pauses; stop at the first provider
    error (usually rate limiting) and try again on the next run. Commits after every row so
    the scrape loop (which re-values the same rows) is never blocked for minutes."""
    from app.storage.listings import apply_costs

    counts = {"translated": 0, "evidence": 0, "failed": 0}
    for row in pending_listings(session, limit):
        try:
            text = translator.translate(row.description or "")
            row.description_en = text.strip() or None
            apply_costs(row, now)  # repair patterns also read the English text
            counts["translated"] += 1
            session.commit()  # one row per transaction: never hold locks across the pauses
            sleep(random.uniform(1.5, 4.0))
            counts["evidence"] += translate_evidence(row, translator)
        except (TranslateError, httpx.HTTPError) as e:
            log.warning("translate.failed", listing=row.id, provider=translator.name, error=str(e))
            counts["failed"] += 1
            return counts
        session.commit()
    # cars translated earlier whose tags gained evidence since (re-tagging, rule updates)
    for row in pending_evidence(session, max(0, limit - counts["translated"])):
        try:
            counts["evidence"] += translate_evidence(row, translator)
        except (TranslateError, httpx.HTTPError) as e:
            log.warning("translate.failed", listing=row.id, provider=translator.name, error=str(e))
            counts["failed"] += 1
            return counts
        session.commit()
        sleep(random.uniform(1.5, 4.0))
    return counts
