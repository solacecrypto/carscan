import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Listing, ListingInsight, SavedSearch, SellerType, User
from app.insights import AnalysisResult, DescriptionInsight, analyze_pending, pending_listings
from app.insights.analyzer import ClaudeAnalyzer, build_user_message
from app.insights.schema import JSON_SCHEMA
from app.scrapers import PoliteClient
from app.storage.filters import apply_sql, matches
from app.worker.jobs import run_saved_search

NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
FIX = Path(__file__).parent / "fixtures"


def _fake(title: str, description: str) -> AnalysisResult:
    if "einen Unfall" in description:
        ins = DescriptionInsight(
            language="de",
            summary="Had an accident.",
            accident="yes",
            accident_evidence="hatte einen Unfall",
            timing_chain="not_mentioned",
            service_history="unknown",
            export_only=True,
            defects=["dent rear left"],
        )
    else:
        ins = DescriptionInsight(
            language="de",
            summary="Clean car.",
            accident="no",
            accident_evidence="unfallfrei",
            timing_chain="replaced",
            service_history="full",
            export_only=False,
            defects=[],
        )
    return AnalysisResult("ok", insight=ins, raw=ins.model_dump(), model="fake")


def _listing(db: Session, sid: str, desc: str | None, **kw: object) -> Listing:
    row = Listing(
        source="autoscout24_de",
        source_listing_id=sid,
        url=f"https://x/{sid}",
        make="BMW",
        model="320i",
        price_eur=5000,
        seller_type=SellerType.dealer,
        country="DE",
        first_seen=NOW,
        last_seen=NOW,
        description=desc,
        **kw,
    )
    db.add(row)
    db.flush()
    return row


def test_analyze_pending_stores_and_skips_unchanged(db: Session) -> None:
    a = _listing(db, "a", "Unfallfrei, Steuerkette neu, Scheckheft gepflegt.")
    b = _listing(db, "b", "Das Auto hatte einen Unfall. Nur Export.")
    _listing(db, "c", None)
    counts = analyze_pending(db, _fake, NOW, limit=10)
    db.commit()
    db.expire_all()
    assert counts == {"ok": 2, "refused": 0, "error": 0}
    assert db.query(ListingInsight).count() == 2
    assert (
        a.insight.accident == "no"
        and a.insight.timing_chain == "replaced"
        and a.insight.service_history == "full"
    )
    assert (
        b.insight.accident == "yes"
        and b.insight.export_only
        and b.insight.defects == ["dent rear left"]
    )
    assert pending_listings(db, 10) == []
    b.description = "Das Auto hatte einen Unfall. Nur Export. Neuer Text."
    db.flush()
    assert [x.id for x in pending_listings(db, 10)] == [b.id]
    assert analyze_pending(db, _fake, NOW, limit=1)["ok"] == 1
    # errors are retried, refusals are not
    a.insight.status = "error"
    b.insight.status = "refused"
    db.flush()
    assert [x.id for x in pending_listings(db, 10)] == [a.id]


def test_filters_use_insights(db: Session) -> None:
    a = _listing(db, "a", "Unfallfrei, Steuerkette neu.")
    b = _listing(db, "b", "hatte einen Unfall")
    c = _listing(db, "c", None)
    analyze_pending(db, _fake, NOW, limit=10)
    db.commit()
    assert (
        matches(a, {"no_accident": 1})
        and not matches(b, {"no_accident": 1})
        and matches(c, {"no_accident": 1})
    )
    assert matches(a, {"chain_replaced": 1}) and not matches(c, {"chain_replaced": 1})
    assert not matches(b, {"no_export_only": 1}) and matches(a, {"service_full": 1})
    ids = {r.id for r in db.scalars(apply_sql(select(Listing), {"no_accident": 1}))}
    assert ids == {a.id, c.id}
    assert {
        r.id
        for r in db.scalars(apply_sql(select(Listing), {"chain_replaced": 1, "service_full": 1}))
    } == {a.id}
    assert {r.id for r in db.scalars(apply_sql(select(Listing), {"no_export_only": 1}))} == {
        a.id,
        c.id,
    }


def test_scrape_run_analyses_new_listings_within_budget(db: Session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    u = User(username="u", password_hash="x")
    s = SavedSearch(name="e90", owner=u, source="autoscout24_de", source_params={"path": "/x"})
    db.add_all([u, s])
    db.commit()
    monkeypatch.setattr("app.worker.jobs.default_analyzer", lambda: _fake)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/angebote/"):
            return httpx.Response(
                200, text=(FIX / "autoscout24_de" / "detail_dealer.html").read_text()
            )
        page = request.url.params.get("page", "1")
        return httpx.Response(
            200,
            text=(
                FIX
                / "autoscout24_de"
                / ("search_page1.html" if page == "1" else "search_empty.html")
            ).read_text(),
        )

    client = PoliteClient(transport=httpx.MockTransport(handler), sleep=lambda _: None)
    r = run_saved_search(s.id, client=client, detail_budget=5, insights_budget=3)
    assert r.status.value == "ok" and r.new == 20
    db.expire_all()
    n = db.scalar(select(ListingInsight).where(ListingInsight.status == "ok"))
    assert n is not None
    assert db.query(ListingInsight).count() == 3  # budget respected; 5 had descriptions


def test_claude_analyzer_request_shape_and_refusal() -> None:
    calls: list[dict] = []

    class FakeMessages:
        def create(self, **kw):  # type: ignore[no-untyped-def]
            calls.append(kw)
            if "refuse" in kw["messages"][0]["content"]:
                return SimpleNamespace(
                    stop_reason="refusal",
                    stop_details=SimpleNamespace(category="general_harms"),
                    content=[],
                    model="m",
                    usage=None,
                )
            data = {
                "language": "it",
                "summary": "ok",
                "accident": "unknown",
                "accident_evidence": "",
                "timing_chain": "not_mentioned",
                "service_history": "partial",
                "export_only": False,
                "defects": ["rust on sill"],
            }
            return SimpleNamespace(
                stop_reason="end_turn",
                stop_details=None,
                content=[SimpleNamespace(type="text", text=json.dumps(data))],
                model="claude-opus-5-5",
                usage=SimpleNamespace(
                    input_tokens=500, output_tokens=80, cache_read_input_tokens=400
                ),
            )

    an = ClaudeAnalyzer(api_key="sk-test", model="claude-opus-5-5")
    an.client = SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages()))
    r = an("BMW 320i", "Qualche ruggine sul brancardo.")
    assert (
        r.status == "ok"
        and r.insight is not None
        and r.insight.defects == ["rust on sill"]
        and r.model == "claude-opus-5-5"
    )
    kw = calls[0]
    assert (
        kw["model"] == "claude-opus-5-5"
        and kw["fallbacks"] == "default"
        and "server-side-fallback-2026-07-01" in kw["betas"]
    )
    assert (
        kw["output_config"]["format"]["schema"] == JSON_SCHEMA
        and kw["output_config"]["effort"] == "low"
    )
    assert (
        kw["system"][0]["cache_control"] == {"type": "ephemeral"}
        and "<description>" in kw["messages"][0]["content"]
    )
    assert an("x", "please refuse").status == "refused"
    assert "truncated" in build_user_message("t", "x" * 20_000)
