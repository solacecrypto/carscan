import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Listing
from app.translate import GoogleTranslator, TranslateError, chunks, translate_pending
from tests.test_insights import NOW, _listing


class FakeTranslator:
    name = "fake"

    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on, self.calls = fail_on, 0

    def translate(self, text: str) -> str:
        self.calls += 1
        if self.fail_on and self.fail_on in text:
            raise TranslateError("boom")
        return f"EN: {text}"


def test_chunks_respect_limit_and_keep_text() -> None:
    text = "Erster Satz. Zweiter Satz!\n" * 50 + "x" * 120
    parts = chunks(text, limit=100)
    assert all(len(p) <= 100 for p in parts)
    assert "".join(parts).replace(" ", "") == text.replace(" ", "")


def test_google_translator_parses_the_public_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "Steuerkette erneuert"
        return httpx.Response(
            200, json=[[["Timing chain replaced", "Steuerkette erneuert"]], None, "de"]
        )

    t = GoogleTranslator(httpx.Client(transport=httpx.MockTransport(handler)))
    assert t.translate("Steuerkette erneuert") == "Timing chain replaced"


def test_translate_pending_best_spread_first_and_stops_on_error(db: Session) -> None:
    a = _listing(db, "a", "Guter Zustand", spread_eur=500)
    b = _listing(db, "b", "Unfallfrei", spread_eur=2000)
    _listing(db, "c", None)
    d = _listing(db, "d", "Motorschaden", spread_eur=None)
    db.commit()
    t = FakeTranslator(fail_on="Motorschaden")
    counts = translate_pending(db, t, NOW, limit=10, sleep=lambda _: None)
    assert counts == {"translated": 2, "failed": 1}
    assert b.description_en == "EN: Unfallfrei" and a.description_en == "EN: Guter Zustand"
    assert d.description_en is None  # retried next run
    assert (
        translate_pending(db, FakeTranslator(), NOW, limit=10, sleep=lambda _: None)["translated"]
        == 1
    )
    assert db.get(Listing, d.id).description_en == "EN: Motorschaden"


def test_new_description_clears_stale_translation(db: Session) -> None:
    from app.storage.listings import upsert_listings
    from tests.test_storage import _item, _search

    search = _search(db)
    upsert_listings(db, [_item("x", 5000, description="Alt")], search, NOW)
    row = db.scalar(select(Listing).where(Listing.source_listing_id == "x"))
    assert row is not None
    row.description_en = "Old"
    db.flush()
    upsert_listings(db, [_item("x", 5000, description="Neu")], search, NOW)
    assert row.description == "Neu" and row.description_en is None
