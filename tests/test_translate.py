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
        return "\n".join(f"EN: {line}" for line in text.split("\n"))


def test_chunks_respect_limit_and_keep_text() -> None:
    text = "Erster Satz. Zweiter Satz!\n" * 50 + "x" * 120
    parts = chunks(text, limit=100)
    assert all(len(p) <= 100 for p in parts)
    assert "".join(parts).replace(" ", "") == text.replace(" ", "")


def test_google_translator_parses_the_public_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and b"q=Steuerkette+erneuert" in request.content
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
    assert counts == {"translated": 2, "evidence": 0, "failed": 1}
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


def test_evidence_is_translated_and_survives_retagging(db: Session) -> None:
    from app.insights.service import apply_rules
    from app.translate import translate_evidence

    row = _listing(db, "ev", "Unfallfrei. Steuerkette wurde erneuert.")
    apply_rules(row, NOW)
    db.flush()
    assert translate_evidence(row, FakeTranslator()) == 2
    ens = {t["key"]: t["evidence_en"] for t in row.insight.tags}
    assert ens["accident_free"].startswith("EN: ") and ens["chain_replaced"].startswith("EN: ")
    apply_rules(row, NOW)  # same text re-tagged on the next scrape: keep the translations
    assert {t["key"]: t.get("evidence_en") for t in row.insight.tags} == ens
    assert translate_evidence(row, FakeTranslator()) == 0


def test_translate_pending_also_fills_evidence_on_older_cars(db: Session) -> None:
    from app.insights.service import apply_rules

    row = _listing(db, "old", "Motorschaden.", description_en="Engine damage.")
    apply_rules(row, NOW)
    db.commit()
    counts = translate_pending(db, FakeTranslator(), NOW, limit=5, sleep=lambda _: None)
    assert counts == {"translated": 0, "evidence": 1, "failed": 0}
    assert row.insight.tags[0]["evidence_en"] == "EN: Motorschaden."


def test_styled_unicode_is_folded_and_rejected_text_is_skipped(db: Session) -> None:
    from app.translate import normalize

    assert normalize("𝐆𝐎𝐋𝐅 𝟏.𝟒 𝐓𝐒𝐈 ｆｕｌｌ") == "GOLF 1.4 TSI full"

    class Rejecting:
        name = "fake"

        def translate(self, text: str) -> str:
            if "bad" in text:
                raise TranslateError("HTTP 400", permanent=True)
            return "EN: " + text

    bad = _listing(db, "bad", "bad text", spread_eur=900)
    good = _listing(db, "good", "fine text", spread_eur=100)
    db.commit()
    counts = translate_pending(db, Rejecting(), NOW, limit=5, sleep=lambda _: None)
    assert counts["translated"] == 1 and counts["failed"] == 1
    assert bad.description_en == "" and good.description_en == "EN: fine text"
    assert translate_pending(db, Rejecting(), NOW, limit=5, sleep=lambda _: None)["failed"] == 0


def test_translate_job_backs_off_after_a_block(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.worker import jobs

    calls: list[int] = []

    def fake_pending(session, translator, now, limit):  # type: ignore[no-untyped-def]
        calls.append(1)
        return {"translated": 0, "evidence": 0, "failed": 1}

    monkeypatch.setattr("app.translate.translate_pending", fake_pending)
    monkeypatch.setattr("app.translate.default_translator", lambda: FakeTranslator())
    monkeypatch.setattr(jobs, "_TRANSLATE_COOLDOWN_UNTIL", jobs._TRANSLATE_COOLDOWN_UNTIL)
    assert jobs.translate_backlog(limit=5)["failed"] == 1
    assert jobs.translate_backlog(limit=5) == {}  # cooling down: no request made
    assert len(calls) == 1
