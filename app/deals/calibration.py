"""Compare estimates frozen on deals with what actually happened, and suggest config values."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.costs import get_cost_config
from app.db.models import Deal, DealStage


@dataclass
class Metric:
    name: str
    n: int
    estimated: float | None
    actual: float | None
    suggestion: str
    current: str = ""
    samples: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CalibrationReport:
    metrics: list[Metric]
    deals_bought: int
    deals_sold: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _med(values: list[float]) -> float | None:
    return float(median(values)) if values else None


def calibrate(session: Session) -> CalibrationReport:
    cfg = get_cost_config()
    deals = list(
        session.scalars(
            select(Deal)
            .where(
                Deal.stage.in_(
                    [DealStage.bought, DealStage.imported, DealStage.listed, DealStage.sold]
                )
            )
            .options(selectinload(Deal.listing))
        )
    )
    bought = [d for d in deals if d.purchase_price_eur is not None]
    sold = [d for d in deals if d.stage is DealStage.sold and d.sale_price_eur is not None]
    metrics: list[Metric] = []

    # transport per country
    by_country: dict[str, list[Deal]] = {}
    for d in bought:
        if d.transport_eur is not None:
            by_country.setdefault(d.listing.country, []).append(d)
    for country, ds in sorted(by_country.items()):
        actual = _med([float(d.transport_eur or 0) for d in ds])
        current = cfg.transport.get(country, cfg.transport.get("DEFAULT"))
        metrics.append(
            Metric(
                name=f"transport {country}",
                n=len(ds),
                estimated=float(current) if current is not None else None,
                actual=actual,
                current=f"transport.{country}: {current}",
                suggestion=f"transport.{country}: {round(actual or 0)}",
            )
        )

    # BPM: actual vs estimate
    bpm = [d for d in bought if d.bpm_eur is not None and d.est_bpm_eur]
    if bpm:
        ratios = [d.bpm_eur / d.est_bpm_eur for d in bpm]  # type: ignore[operator]
        r = _med(ratios) or 1.0
        metrics.append(
            Metric(
                name="BPM (actual ÷ estimate)",
                n=len(bpm),
                estimated=_med([float(d.est_bpm_eur or 0) for d in bpm]),
                actual=_med([float(d.bpm_eur or 0) for d in bpm]),
                current="bpm tables in costs.yaml",
                suggestion=(
                    "estimates are within 10%: keep the tables"
                    if abs(r - 1) < 0.1
                    else f"actual BPM is {r:.2f}× the estimate: check tariff tables / CO2 fallbacks"
                ),
                samples=[
                    {"deal": d.id, "estimate": d.est_bpm_eur, "actual": d.bpm_eur} for d in bpm
                ],
            )
        )

    # fees
    fees = [d for d in bought if d.fees_eur is not None]
    if fees:
        actual = _med([float(d.fees_eur or 0) for d in fees])
        current = float(sum(cfg.fixed_fees.values()))
        metrics.append(
            Metric(
                name="RDW and fees",
                n=len(fees),
                estimated=current,
                actual=actual,
                current=f"fixed_fees total: {current:.0f}",
                suggestion=f"scale fixed_fees so they sum to {round(actual or 0)}",
            )
        )

    # purchase price vs asking price
    pp = [d for d in bought if d.est_price_eur]
    if pp:
        disc = [1 - d.purchase_price_eur / d.est_price_eur for d in pp]  # type: ignore[operator]
        metrics.append(
            Metric(
                name="negotiated discount on asking price",
                n=len(pp),
                estimated=0.0,
                actual=round(100 * (_med(disc) or 0), 1),
                current="landed cost uses the asking price",
                suggestion=f"typical discount {100 * (_med(disc) or 0):.1f}% (informational)",
            )
        )

    # haircut: sale price vs NL asking median
    hc = [d for d in sold if d.est_nl_asking_median_eur]
    if hc:
        ratios = [d.sale_price_eur / d.est_nl_asking_median_eur for d in hc]  # type: ignore[operator]
        suggested = round(100 * (1 - (_med(ratios) or 1)), 1)
        metrics.append(
            Metric(
                name="haircut (asking → sale)",
                n=len(hc),
                estimated=float(cfg.valuation.get("haircut_pct", 0)),
                actual=suggested,
                current=f"valuation.haircut_pct: {cfg.valuation.get('haircut_pct')}",
                suggestion=f"valuation.haircut_pct: {suggested}",
                samples=[
                    {
                        "deal": d.id,
                        "asking_median": d.est_nl_asking_median_eur,
                        "sale": d.sale_price_eur,
                    }
                    for d in hc
                ],
            )
        )

    # NL value estimate vs sale price
    nv = [d for d in sold if d.est_nl_value_eur]
    if nv:
        metrics.append(
            Metric(
                name="NL value estimate vs sale price",
                n=len(nv),
                estimated=_med([float(d.est_nl_value_eur or 0) for d in nv]),
                actual=_med([float(d.sale_price_eur or 0) for d in nv]),
                current="",
                suggestion="see haircut",
            )
        )

    # days to sell, profit
    dts = [d.days_to_sell for d in sold if d.days_to_sell is not None]
    if dts:
        metrics.append(
            Metric(
                name="days to sell",
                n=len(dts),
                estimated=None,
                actual=_med([float(x) for x in dts]),
                suggestion="informational",
            )
        )
    profits = [d.profit_eur for d in sold if d.profit_eur is not None]
    if profits:
        metrics.append(
            Metric(
                name="profit per sold car",
                n=len(profits),
                estimated=_med(
                    [float(d.est_spread_eur or 0) for d in sold if d.est_spread_eur is not None]
                ),
                actual=_med([float(p) for p in profits]),
                suggestion="estimated spread vs realised profit (informational)",
            )
        )
    return CalibrationReport(metrics=metrics, deals_bought=len(bought), deals_sold=len(sold))
