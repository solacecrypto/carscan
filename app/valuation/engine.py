"""NL value estimate from comparable asking prices. Pure functions, no DB."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from statistics import median
from typing import Any


@dataclass(frozen=True)
class Comp:
    id: int
    source: str
    model: str
    body_type: str | None
    year: int | None
    mileage_km: int | None
    price_eur: int
    url: str = ""
    variant: str | None = None


@dataclass(frozen=True)
class Target:
    model: str
    body_type: str | None
    year: int | None
    mileage_km: int | None


@dataclass(frozen=True)
class ValueEstimate:
    value_eur: int
    asking_median_eur: int
    asking_q1_eur: int
    asking_q3_eur: int
    n: int
    year_window: int
    mileage_window_km: int
    haircut_pct: float
    km_rate_eur: float
    nearest: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["nearest"] = list(self.nearest)
        return d


@dataclass(frozen=True)
class ValuationConfig:
    haircut_pct: float = 7.0
    km_rate_eur: float = 0.02
    min_comps: int = 5
    year_window: int = 1
    year_window_max: int = 3
    mileage_window_km: int = 40_000
    mileage_window_max_km: int = 120_000
    nearest_to_show: int = 8

    @classmethod
    def from_mapping(cls, m: dict[str, Decimal]) -> ValuationConfig:
        kw: dict[str, Any] = {}
        for f, typ in (
            ("haircut_pct", float),
            ("km_rate_eur", float),
            ("min_comps", int),
            ("year_window", int),
            ("year_window_max", int),
            ("mileage_window_km", int),
            ("mileage_window_max_km", int),
            ("nearest_to_show", int),
        ):
            if f in m:
                kw[f] = typ(m[f])
        return cls(**kw)


def dedup(comps: list[Comp]) -> list[Comp]:
    """The same car is often on several NL sites; keep one per (model, year, ~km, price)."""
    seen: set[tuple[str, str | None, int | None, int | None, int]] = set()
    out: list[Comp] = []
    for c in comps:
        key = (
            c.model.lower(),
            c.body_type,
            c.year,
            (c.mileage_km // 2000) if c.mileage_km is not None else None,
            c.price_eur,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _quartile(values: list[float], q: float) -> float:
    s = sorted(values)
    if not s:
        return 0.0
    pos = (len(s) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def estimate_value(target: Target, comps: list[Comp], cfg: ValuationConfig) -> ValueEstimate | None:
    if target.year is None:
        return None
    pool_all = [
        c
        for c in dedup(comps)
        if c.model.lower() == target.model.lower()
        and c.year is not None
        and (target.body_type is None or c.body_type is None or c.body_type == target.body_type)
    ]
    chosen: list[Comp] = []
    yw, mw = cfg.year_window, cfg.mileage_window_km
    windows = [
        (y, m)
        for y in range(cfg.year_window, cfg.year_window_max + 1)
        for m in range(
            cfg.mileage_window_km, cfg.mileage_window_max_km + 1, max(1, cfg.mileage_window_km)
        )
    ]
    windows.sort(key=lambda t: (t[0] + t[1] / cfg.mileage_window_max_km, t[1]))
    for yw, mw in windows:
        chosen = [
            c
            for c in pool_all
            if abs(c.year - target.year) <= yw  # type: ignore[operator]
            and (
                target.mileage_km is None
                or c.mileage_km is None
                or abs(c.mileage_km - target.mileage_km) <= mw
            )
        ]
        if len(chosen) >= cfg.min_comps:
            break
    if len(chosen) < cfg.min_comps:
        return None

    def adjusted(c: Comp) -> float:
        if target.mileage_km is None or c.mileage_km is None:
            return float(c.price_eur)
        return c.price_eur + (c.mileage_km - target.mileage_km) * cfg.km_rate_eur

    prices = [adjusted(c) for c in chosen]
    med = median(prices)
    value = med * (1 - cfg.haircut_pct / 100)
    nearest = sorted(
        chosen,
        key=lambda c: (
            abs(c.year - target.year),  # type: ignore[operator]
            abs((c.mileage_km or 0) - (target.mileage_km or 0)),
        ),
    )[: cfg.nearest_to_show]
    return ValueEstimate(
        value_eur=round(value),
        asking_median_eur=round(med),
        asking_q1_eur=round(_quartile(prices, 0.25)),
        asking_q3_eur=round(_quartile(prices, 0.75)),
        n=len(chosen),
        year_window=yw,
        mileage_window_km=mw,
        haircut_pct=cfg.haircut_pct,
        km_rate_eur=cfg.km_rate_eur,
        nearest=tuple(
            {
                "id": c.id,
                "source": c.source,
                "year": c.year,
                "mileage_km": c.mileage_km,
                "price_eur": c.price_eur,
                "adjusted_eur": round(adjusted(c)),
                "url": c.url,
                "variant": c.variant,
            }
            for c in nearest
        ),
    )
