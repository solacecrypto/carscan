"""Landed cost = purchase price + transport + BPM + fixed fees."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.costs.bpm import BpmEstimate, estimate_bpm
from app.costs.config import CostConfig, get_cost_config


@dataclass(frozen=True)
class LandedCost:
    purchase_eur: int
    transport_eur: int
    bpm_eur: int | None  # None = could not be estimated
    fees_eur: int
    total_eur: int | None
    bpm: BpmEstimate | None
    config_version: int
    fees: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.bpm is not None:
            d["bpm"]["depreciation_pct"] = float(self.bpm.depreciation_pct)
            d["bpm"]["notes"] = list(self.bpm.notes)
        return d


def _euros(x: Decimal) -> int:
    return int(x.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def landed_cost(
    *,
    price_eur: int,
    country: str,
    first_registration: date | None,
    co2_g_km: int | None,
    fuel: str | None,
    make: str,
    model: str,
    cfg: CostConfig | None = None,
    today: date | None = None,
) -> LandedCost:
    cfg = cfg or get_cost_config()
    today = today or date.today()
    transport = cfg.transport.get(country.upper(), cfg.transport.get("DEFAULT", Decimal(0)))
    fees = {k: _euros(v) for k, v in cfg.fixed_fees.items() if v}
    fees_total = sum(fees.values())
    bpm = estimate_bpm(
        first_registration=first_registration,
        co2_g_km=co2_g_km,
        fuel=fuel,
        make=make,
        model=model,
        cfg=cfg,
        today=today,
    )
    bpm_eur = bpm.net_eur if bpm else None
    total = price_eur + _euros(transport) + fees_total + bpm_eur if bpm_eur is not None else None
    return LandedCost(
        purchase_eur=price_eur,
        transport_eur=_euros(transport),
        bpm_eur=bpm_eur,
        fees_eur=fees_total,
        total_eur=total,
        bpm=bpm,
        config_version=cfg.version,
        fees=fees,
    )
