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
    assert db.query(ListingInsight).filter_by(status="ok").count() == 3  # Claude budget respected
    assert db.query(ListingInsight).count() == 5  # the other two got rule-based tags


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


def test_enrichment_gating_by_provisional_spread(db: Session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from datetime import date

    from app.db import Comparable
    from app.normalize import NormalizedListing, RawListing
    from app.worker.jobs import _select_for_enrichment

    # six NL comparables at ~€9,000 for 320i estates of 2012
    for i in range(6):
        db.add(
            Comparable(
                source="autoscout24_nl",
                source_listing_id=f"c{i}",
                url="u",
                make="BMW",
                model="320i",
                body_type="estate",
                first_registration=date(2012, 1, 1),
                mileage_km=150_000 + i * 1000,
                price_eur=9000 + i * 100,
                seller_type=SellerType.dealer,
                first_seen=NOW,
                last_seen=NOW,
                is_active=True,
            )
        )
    db.commit()

    def item(sid: str, price: int, model: str = "320i") -> NormalizedListing:
        return NormalizedListing(
            source="autoscout24_de",
            source_listing_id=sid,
            url="u",
            make="BMW",
            model=model,
            body_type="estate",
            price_eur=price,
            mileage_km=150_000,
            first_registration=date(2012, 6, 1),
            fuel="petrol",
            seller_type=SellerType.dealer,
            country="DE",
        )

    raws = [
        RawListing(source="autoscout24_de", source_listing_id=s, url="u", data={}, fetched_at=NOW)
        for s in ("cheap", "dear", "unknown")
    ]
    items = [item("cheap", 4000), item("dear", 9500), item("unknown", 4000, model="520i")]
    chosen = _select_for_enrichment(db, raws, items, 0)
    assert [r.source_listing_id for r in chosen] == [
        "cheap",
        "unknown",
    ]  # dear car has negative spread; unknown model has no comps
    assert len(_select_for_enrichment(db, raws, items, None)) == 3


def test_rule_tags_and_claude_precedence(db: Session) -> None:
    from app.insights import apply_rules
    from app.insights.rules import extract_tags

    text = (
        "Unfallfrei, Steuerkette bei 150.000 km erneuert, TÜV neu bis 03/2027. "
        "Scheckheftgepflegt. Leider Klimaanlage defekt."
    )
    tags = extract_tags(text)
    assert [t.key for t in tags] == [
        "ac_broken",
        "chain_replaced",
        "accident_free",
        "tuv_new",
        "full_history",
    ]
    assert tags[1].evidence.startswith("Unfallfrei, Steuerkette bei 150.000 km erneuert")
    assert [
        t.key
        for t in extract_tags("Auto a fatto incidente nel 2019, catena sostituita. Solo export.")
    ] == ["accident", "export_only", "chain_replaced"]
    assert (
        extract_tags("Schadevrij en zuinig.")[0].key == "accident_free"
    )  # 'schade' inside schadevrij is not an accident

    a = _listing(db, "r1", "Unfallfrei, Steuerkette neu. Klimaanlage defekt.")
    ins = apply_rules(a, NOW)
    db.commit()
    assert (
        ins is not None
        and ins.status == "rules"
        and ins.accident == "no"
        and ins.timing_chain == "replaced"
    )
    assert ins.defects == ["air conditioning broken"] and [t["key"] for t in ins.tags] == [
        "ac_broken",
        "chain_replaced",
        "accident_free",
    ]
    # Claude result replaces the rule layer but keeps rule evidence and neutral notes
    analyze_pending(db, _fake, NOW, limit=5)
    db.commit()
    db.refresh(a)
    assert (
        a.insight.status == "ok"
        and a.insight.tags[0]["key"] == "accident_free"
        and a.insight.tags[0]["evidence"] == "unfallfrei"
    )
    assert {t["key"] for t in a.insight.tags} >= {"chain_replaced", "full_history"}
    # unchanged text: rules do not overwrite a current Claude result
    assert apply_rules(a, NOW).status == "ok"
    assert apply_rules(_listing(db, "r2", "   "), NOW) is None


def test_kleinanzeigen_ad_page_detail() -> None:
    from datetime import date

    from app.normalize import RawListing
    from app.scrapers import get_scraper
    from app.scrapers.kleinanzeigen import parse_ad_page, parse_articles

    det = parse_ad_page((FIX / "kleinanzeigen" / "ad_page.html").read_text())
    assert (
        det["description"].startswith("Das Auto befindet sich technisch")
        and len(det["description"]) > 200
    )
    assert det["details"]["Getriebe"] == "Manuell" and det["details"]["Leistung"].endswith("PS")
    items = parse_articles((FIX / "kleinanzeigen" / "search_page1.html").read_text())
    item = next(i for i in items if i["id"] == "3537159648")
    raw = RawListing(
        source="kleinanzeigen",
        source_listing_id=item["id"],
        url="https://www.kleinanzeigen.de" + item["href"],
        data=item,
        fetched_at=NOW,
        detail=det,
    )
    n = get_scraper("kleinanzeigen").parse(raw)
    assert (
        n.gearbox == "manual"
        and n.power_kw == 135
        and n.first_registration == date(2012, 7, 1)
        and "Unfall" in (n.description or "")
    )
    assert n.raw["detail"]["details"]["Fahrzeugzustand"]


def test_enrich_backlog_fetches_missing_ad_pages(db: Session) -> None:
    from app.db import SavedSearch, User
    from app.scrapers.kleinanzeigen import parse_articles
    from app.worker.jobs import enrich_backlog

    u = User(username="u", password_hash="x")
    search = SavedSearch(
        name="ka",
        owner=u,
        source="kleinanzeigen",
        source_params={"make": "bmw", "make_label": "BMW", "model_key": "bmw-3er-e90"},
    )
    db.add_all([u, search])
    db.flush()
    items = parse_articles((FIX / "kleinanzeigen" / "search_page1.html").read_text())
    item = next(i for i in items if i["id"] == "3537159648")
    old = _listing(db, item["id"], None, raw={"search": item}, saved_searches=[search])
    old.source = "kleinanzeigen"
    old.url = "https://www.kleinanzeigen.de" + item["href"]
    db.commit()
    page = (FIX / "kleinanzeigen" / "ad_page.html").read_text()
    client = PoliteClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page)),
        sleep=lambda _: None,
    )
    counts = enrich_backlog(limit=10, client=client)
    assert counts["fetched"] == 1
    db.expire_all()
    row = db.get(Listing, old.id)
    assert (
        row is not None
        and row.description
        and "Unfall" in row.description
        and row.gearbox == "manual"
    )
    assert row.insight is not None and any(t["key"] == "accident" for t in row.insight.tags)
    assert enrich_backlog(limit=10, client=client)["fetched"] == 0  # nothing left to fetch


def test_oil_consumption_tag_from_title_and_spaced_wording(db: Session) -> None:
    from app.insights.service import apply_rules

    row = _listing(db, "oil1", "Fahrzeug hat starken Öl Verbrauch, 1 Liter auf 300km!")
    row.variant = "318i Touring / ÖLVERBRAUCH"
    apply_rules(row, NOW)
    assert [t["key"] for t in row.insight.tags] == ["oil_consumption"]


def test_paperwork_tags_and_tuv_validity() -> None:
    from app.insights.rules import extract_tags

    text = (
        "TÜV bis 06/2027, Fahrzeugbrief und Fahrzeugschein vorhanden, COC Papier dabei, "
        "TÜV-Bericht liegt bei."
    )
    tags = {t.key: t for t in extract_tags(text)}
    assert tags["tuv_new"].until == "06/2027"
    assert tags["tuv_new"].label == "TÜV / inspection until 06/2027"
    assert {"papers_teil1", "papers_teil2", "papers_coc", "tuv_report"} <= set(tags)
    assert extract_tags("HU 03/28 neu")[0].until == "03/2028"
    assert extract_tags("TÜV neu bis 2028")[0].until == "12/2028"
    assert extract_tags("TÜV neu")[0].until is None
    assert extract_tags("TÜV 13/2027")[0].until is None  # not a month
