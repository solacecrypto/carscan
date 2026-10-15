from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.costs.repairs import estimate_repairs, find_items

NOW = datetime(2026, 10, 15, tzinfo=UTC)

VOLVO_DE = """Auto muss mit Abschleppwagen abgeholt werden!!
Hinweis zum Zustand: Das Fahrzeug ist nicht fahrbereit. Soweit mir bekannt liegt der Defekt an
den Injektoren. Der Volvo wird ausdrücklich als Bastlerfahrzeug verkauft. TÜV bis 2027."""
VOLVO_EN = """Car must be picked up with a tow truck!! The vehicle is not drivable.
As far as I know, the defect is in the injectors. Sold as a hobbyist vehicle."""


def test_finds_defects_in_german_and_english() -> None:
    de = find_items(VOLVO_DE)
    assert set(de) == {"not_drivable", "injectors", "unspecified"}
    assert "Injektoren" in de["injectors"]
    en = find_items(VOLVO_EN)
    assert {"not_drivable", "injectors", "unspecified"} <= set(en)


def test_estimate_combines_texts_and_drops_unspecified_when_specific() -> None:
    est = estimate_repairs("Volvo V50 1.6 Diesel | Bastlerfahrzeug", VOLVO_DE, VOLVO_EN)
    assert est is not None
    keys = [i["key"] for i in est["items"]]
    assert "injectors" in keys and "not_drivable" in keys and "unspecified" not in keys
    assert est["low"] == 200 + 600 and est["high"] == 400 + 1500
    assert est["estimate"] == round((800 + 1900) / 2)
    # config ranges override the defaults
    est2 = estimate_repairs(VOLVO_DE, ranges={"injectors": (100, 200)})
    assert next(i for i in est2["items"] if i["key"] == "injectors")["high"] == 200


def test_negations_and_clean_cars() -> None:
    assert find_items("Kein Rost, keine Mängel, TÜV neu, unfallfrei.") == {}
    assert find_items("Geen roest en geen olieverbruik. Airco werkt perfect.") == {}
    assert find_items("No rust at all, timing chain replaced at 120k.") == {}
    assert estimate_repairs("Scheckheftgepflegt, Steuerkette neu, keine Mängel") is None
    assert "rust" in find_items("Rost an den Radläufen, Klima funktioniert nicht")
    assert "ac_broken" in find_items("Rost an den Radläufen, Klima funktioniert nicht")
    assert "warning_light" in find_items("Motorkontrollleuchte leuchtet gelegentlich")
    assert "timing_chain_due" in find_items("Steuerkette rasselt beim Kaltstart")
    assert "tuv_expired" in find_items("TÜV abgelaufen seit 03/2025")
    assert "clutch" in find_items("De koppeling slipt bij optrekken")


def test_apply_costs_adds_repairs_to_landed_cost(db: Session) -> None:
    from datetime import date

    from sqlalchemy import select

    from app.db import Listing
    from app.storage.listings import apply_costs, upsert_listings
    from tests.test_storage import _item, _search

    search = _search(db)
    reg = {"first_registration": date(2011, 5, 1), "fuel": "petrol"}
    upsert_listings(
        db, [_item("r1", 3000, description="Motorschaden, nicht fahrbereit.", **reg)], search, NOW
    )
    upsert_listings(
        db, [_item("r2", 3000, description="Top Zustand, keine Mängel.", **reg)], search, NOW
    )
    r1 = db.scalar(select(Listing).where(Listing.source_listing_id == "r1"))
    r2 = db.scalar(select(Listing).where(Listing.source_listing_id == "r2"))
    assert r1 and r2 and r1.repair_cost_eur and r2.repair_cost_eur is None
    assert r1.landed_cost_eur == r2.landed_cost_eur + r1.repair_cost_eur
    assert r1.cost_breakdown["total_with_repairs_eur"] == r1.landed_cost_eur
    assert r1.cost_breakdown["total_eur"] == r2.landed_cost_eur
    keys = {i["key"] for i in r1.repair_breakdown["items"]}
    assert keys == {"engine_damage", "not_drivable"}
    # the English text can add what the original pattern list missed
    r2.description_en = "The clutch is slipping and needs replacing."
    apply_costs(r2, NOW)
    assert r2.repair_cost_eur and "clutch" in {i["key"] for i in r2.repair_breakdown["items"]}


def test_equipment_lists_do_not_trigger_repairs() -> None:
    clean = [
        "Volkswagen passat automaat van eerste eigenaar met volledige historie",
        "Aktiv-Lautsprecher, Auspuffblende, Anhängerbetrieb laut Fahrzeugpapieren",
        "Pro Line Business Leer Xenon, Partikelfilter: ja, Elektrische Fensterheber",
        "Via delle Rose 12, carrozzeria perfetta, electric window regulator front and rear",
        "Ablage-Paket, Audio-Navigationssystem mit integrierter Handyvorrüstung",
    ]
    for text in clean:
        assert find_items(text) == {}, text
    assert "dpf_egr" in find_items("Partikelfilter ist voll, Warnleuchte an")
    assert "battery" in find_items("Batterie ist leer und muss erneuert werden")
    assert "exhaust" in find_items("Auspuff ist undicht")


def test_renewed_parts_are_not_defects() -> None:
    assert find_items("Neue Stoßdämpfer und Federn hinten, neue Kupplung bei 180.000 km") == {}
    assert find_items("New clutch fitted, shock absorbers replaced last year") == {}
    assert "suspension" in find_items("Stoßdämpfer hinten undicht, Querlenker ausgeschlagen")
    assert "suspension" in find_items("Rear shock absorbers leak and the suspension knocks")
