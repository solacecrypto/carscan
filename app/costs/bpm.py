"""BPM estimate for a used petrol import, per docs/landed_cost.md."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.costs.config import CostConfig, Tariff


@dataclass(frozen=True)
class BpmEstimate:
    bruto_eur: int
    depreciation_pct: Decimal
    net_eur: int
    tariff_year: int | None
    age_months: int
    co2_g_km: int
    co2_estimated: bool
    notes: tuple[str, ...] = ()


def months_between(start: date, end: date) -> int:
    """Whole months from `start` to `end` (a started month counts, as the table prescribes)."""
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day > start.day:
        months += 1
    return max(0, months)


def bruto_bpm(co2: int, tariff: Tariff) -> Decimal:
    total = tariff.fixed
    lower = 0
    for upper, rate in tariff.brackets:
        if upper is None or co2 <= upper:
            total += rate * (co2 - lower)
            break
        total += rate * (upper - lower)
        lower = upper
    return total


def depreciation_pct(age_months: int, cfg: CostConfig) -> Decimal:
    if age_months >= cfg.zero_after_months:
        return Decimal(100)
    row = cfg.depreciation_table[0]
    for candidate in cfg.depreciation_table:
        if age_months >= candidate[0]:
            row = candidate
    start, base, per_month = row
    return min(Decimal(100), base + per_month * (age_months - start))


def estimate_bpm(
    *,
    first_registration: date | None,
    co2_g_km: int | None,
    fuel: str | None,
    make: str,
    model: str,
    cfg: CostConfig,
    today: date,
) -> BpmEstimate | None:
    """Returns None when no estimate is possible (no first registration date, non-petrol)."""
    notes: list[str] = []
    if first_registration is None:
        return None
    if fuel and fuel != "petrol":
        return None  # diesel/EV tariffs are not configured in phase 2
    co2_estimated = co2_g_km is None or co2_g_km <= 0
    if co2_estimated:
        key = f"{make} {model}".lower()
        co2_g_km = cfg.co2_fallback.get(key, cfg.co2_fallback.get("default", 170))
        notes.append(f"CO2 unknown, assumed {co2_g_km} g/km for {key}")
    assert co2_g_km is not None
    age = months_between(first_registration, today)
    pct = depreciation_pct(age, cfg)
    if pct >= 100:
        return BpmEstimate(0, Decimal(100), 0, None, age, co2_g_km, co2_estimated, tuple(notes))
    # most favourable tariff that applied between first registration and today
    candidates = [t for t in cfg.tariffs if first_registration.year <= t.year <= today.year]
    if not candidates:
        candidates = list(cfg.tariffs)
        notes.append("no tariff year inside the registration window, used all configured tariffs")
    best = min(candidates, key=lambda t: bruto_bpm(co2_g_km, t))
    bruto = bruto_bpm(co2_g_km, best)
    net = (bruto * (Decimal(100) - pct) / Decimal(100)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return BpmEstimate(
        bruto_eur=int(bruto.quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
        depreciation_pct=pct.quantize(Decimal("0.01")),
        net_eur=int(net),
        tariff_year=best.year,
        age_months=age,
        co2_g_km=co2_g_km,
        co2_estimated=co2_estimated,
        notes=tuple(notes),
    )
