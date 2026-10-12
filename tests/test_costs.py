from datetime import date
from decimal import Decimal

from app.costs import (
    bruto_bpm,
    depreciation_pct,
    estimate_bpm,
    get_cost_config,
    landed_cost,
    months_between,
)

CFG = get_cost_config()
TODAY = date(2026, 10, 12)


def tariff(year: int):  # type: ignore[no-untyped-def]
    return next(t for t in CFG.tariffs if t.year == year)


def test_bruto_bpm_2013_hand_checked() -> None:
    # 158 g/km: (140-95)*125 + (158-140)*148 = 5625 + 2664
    assert bruto_bpm(158, tariff(2013)) == Decimal(8289)
    assert bruto_bpm(95, tariff(2013)) == 0
    assert bruto_bpm(250, tariff(2013)) == Decimal(
        (140 - 95) * 125 + (208 - 140) * 148 + (229 - 208) * 276 + (250 - 229) * 551
    )


def test_bruto_bpm_2015_has_fixed_part() -> None:
    # 175 + 82*6 + 28*69 + 48*112
    assert bruto_bpm(158, tariff(2015)) == Decimal(175 + 492 + 1932 + 5376)


def test_months_between_counts_started_month() -> None:
    assert months_between(date(2010, 5, 1), date(2010, 5, 1)) == 0
    assert months_between(date(2010, 5, 1), date(2010, 6, 1)) == 1
    assert months_between(date(2010, 5, 1), date(2010, 6, 2)) == 2
    assert months_between(date(2014, 10, 1), TODAY) == 145


def test_depreciation_table_rows() -> None:
    assert depreciation_pct(0, CFG) == 0
    assert depreciation_pct(2, CFG) == Decimal(16)  # 12 + 1*4
    assert depreciation_pct(114, CFG) == Decimal(81)
    assert depreciation_pct(145, CFG) == Decimal("86.89")  # 81 + 31*0.19
    assert depreciation_pct(213, CFG) < 100 and depreciation_pct(214, CFG) == 100


def test_estimate_picks_cheapest_tariff_and_depreciates() -> None:
    e = estimate_bpm(
        first_registration=date(2014, 10, 1),
        co2_g_km=158,
        fuel="petrol",
        make="BMW",
        model="320i",
        cfg=CFG,
        today=TODAY,
    )
    assert e is not None
    assert e.tariff_year == 2015 and e.bruto_eur == 7975 and not e.co2_estimated
    assert e.depreciation_pct == Decimal("86.89") and e.net_eur == round(7975 * (100 - 86.89) / 100)


def test_estimate_zero_for_old_cars_and_fallback_co2() -> None:
    old = estimate_bpm(
        first_registration=date(2008, 6, 1),
        co2_g_km=None,
        fuel="petrol",
        make="BMW",
        model="318i",
        cfg=CFG,
        today=TODAY,
    )
    assert (
        old is not None
        and old.net_eur == 0
        and old.depreciation_pct == 100
        and old.tariff_year is None
    )
    assert old.co2_estimated and old.co2_g_km == 146 and "assumed 146" in old.notes[0]
    assert (
        estimate_bpm(
            first_registration=None,
            co2_g_km=150,
            fuel="petrol",
            make="BMW",
            model="320i",
            cfg=CFG,
            today=TODAY,
        )
        is None
    )
    assert (
        estimate_bpm(
            first_registration=date(2012, 1, 1),
            co2_g_km=120,
            fuel="diesel",
            make="BMW",
            model="320d",
            cfg=CFG,
            today=TODAY,
        )
        is None
    )


def test_landed_cost_adds_everything() -> None:
    lc = landed_cost(
        price_eur=6000,
        country="DE",
        first_registration=date(2014, 10, 1),
        co2_g_km=158,
        fuel="petrol",
        make="BMW",
        model="320i",
        cfg=CFG,
        today=TODAY,
    )
    assert lc.transport_eur == 350 and lc.fees_eur == 59 + 50 + 13 + 40 + 20 + 120
    assert lc.bpm_eur == 1046 and lc.total_eur == 6000 + 350 + 302 + 1046
    d = lc.as_dict()
    assert (
        d["bpm"]["tariff_year"] == 2015
        and d["config_version"] == 1
        and d["fees"]["rdw_identificatie"] == 59
    )

    unknown = landed_cost(
        price_eur=6000,
        country="SE",
        first_registration=None,
        co2_g_km=None,
        fuel="petrol",
        make="X",
        model="Y",
        cfg=CFG,
        today=TODAY,
    )
    assert unknown.transport_eur == 1100 and unknown.bpm_eur is None and unknown.total_eur is None
