"""NL value estimate from comparable asking prices. Pure functions, no DB."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from decimal import Decimal
from statistics import median
from typing import Any

# Engine designation as sellers write it, in priority order: "318i" / "318 i", Mercedes
# "C 180" / "E200", "1.4 TSI" / "2.0T", Volvo "T4" / "D2". Power figures (118 kW) are skipped.
_ENGINE_PATTERNS = (
    re.compile(r"\b(\d{3})\s?([a-z]{1,2})\b(?<!kw)(?<!pk)(?<!ps)(?<!hp)(?<!cv)(?<!km)"),
    re.compile(r"\b([abcegs])\s?(\d{3})\b"),
    re.compile(r"\b(\d\.\d)\s?(tsi|tfsi|tdi|fsi|cgi|cdi|t)?\b"),
    re.compile(r"\b([td][2-6])\b"),
)


def engine_token(*texts: str | None) -> str | None:
    """Normalised engine designation found in the given texts (model, variant), or None."""
    blob = " ".join(t for t in texts if t).lower()
    for pat in _ENGINE_PATTERNS:
        m = pat.search(blob)
        if m:
            return "".join(g for g in m.groups() if g)
    return None


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
    engine: str | None = None
    gearbox: str | None = None
    fuel: str | None = None
    power_kw: int | None = None


@dataclass(frozen=True)
class Target:
    model: str
    body_type: str | None
    year: int | None
    mileage_km: int | None
    engine: str | None = None
    gearbox: str | None = None
    fuel: str | None = None
    power_kw: int | None = None


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
    method: str = "median"  # "benchmark" when a close match sets the value
    benchmark: dict[str, Any] | None = None
    median_value_eur: int | None = None  # the median-based value, for context

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
    # a "close match" must share the engine and gearbox (when both known) and sit within:
    close_year_window: int = 1
    close_mileage_pct: float = 15.0
    close_mileage_km: int = 20_000  # or this many km, whichever is larger
    close_power_kw: int = 15
    close_max_deviation_pct: float = 30.0  # benchmark must sit this close to the median

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
            ("close_year_window", int),
            ("close_mileage_pct", float),
            ("close_mileage_km", int),
            ("close_power_kw", int),
            ("close_max_deviation_pct", float),
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


def match_facts(target: Target, c: Comp, cfg: ValuationConfig) -> dict[str, Any] | None:
    """Why a comparable counts as a close match of the target, or None when it does not.

    The engine designation must be known on both sides and equal; gearbox, fuel and power
    only count when both sides know them; year and mileage must be within the close windows.
    ``perfect`` = same year and mileage within 5%."""
    if target.year is None or c.year is None or abs(c.year - target.year) > cfg.close_year_window:
        return None
    if not target.engine or target.engine != c.engine:
        return None
    if target.gearbox and c.gearbox and target.gearbox != c.gearbox:
        return None
    if target.fuel and c.fuel and target.fuel != c.fuel:
        return None
    if target.power_kw and c.power_kw and abs(target.power_kw - c.power_kw) > cfg.close_power_kw:
        return None
    km_diff = None
    if target.mileage_km is not None and c.mileage_km is not None:
        km_diff = c.mileage_km - target.mileage_km
        allowed = max(cfg.close_mileage_km, target.mileage_km * cfg.close_mileage_pct / 100)
        if abs(km_diff) > allowed:
            return None
    elif target.mileage_km is not None:
        return None  # unknown mileage can't be called close
    facts = {
        "same_engine": bool(target.engine and c.engine and target.engine == c.engine),
        "same_gearbox": bool(target.gearbox and c.gearbox and target.gearbox == c.gearbox),
        "same_year": c.year == target.year,
        "year_diff": c.year - target.year,
        "km_diff": km_diff,
        "power_diff_kw": (c.power_kw - target.power_kw) if target.power_kw and c.power_kw else None,
    }
    facts["perfect"] = bool(
        facts["same_year"]
        and facts["same_engine"]
        and (km_diff is not None and abs(km_diff) <= 0.05 * (target.mileage_km or 1))
    )
    return facts


def _closeness(target: Target, c: Comp, facts: dict[str, Any]) -> tuple[float, ...]:
    """Sort key: more shared facts and smaller gaps first."""
    return (
        0 if facts["perfect"] else 1,
        -(int(facts["same_engine"]) + int(facts["same_gearbox"])),
        abs(facts["year_diff"]),
        abs(facts["km_diff"]) if facts["km_diff"] is not None else 10**9,
        abs(facts["power_diff_kw"]) if facts["power_diff_kw"] is not None else 10**3,
    )


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
    median_value = med * (1 - cfg.haircut_pct / 100)

    # One clear benchmark when a close match exists: the same car, in NL, mileage-adjusted,
    # minus the same haircut. The median stays available for context.
    close = [(c, f) for c in pool_all if (f := match_facts(target, c, cfg)) is not None]
    close.sort(key=lambda t: _closeness(target, t[0], t[1]))
    benchmark: dict[str, Any] | None = None
    value = median_value
    # a mislabelled or absurdly priced "match" must not set the value: the benchmark has to
    # sit within close_max_deviation_pct of the reference (median of the close matches when
    # there are at least three, else of the whole pool)
    reference = median([adjusted(c) for c, _ in close]) if len(close) >= 3 else med
    tolerance = reference * cfg.close_max_deviation_pct / 100
    close = [(c, f) for c, f in close if abs(adjusted(c) - reference) <= tolerance]
    if close:
        best, facts = close[0]
        value = adjusted(best) * (1 - cfg.haircut_pct / 100)
        benchmark = {
            "id": best.id,
            "source": best.source,
            "url": best.url,
            "variant": best.variant,
            "year": best.year,
            "mileage_km": best.mileage_km,
            "price_eur": best.price_eur,
            "adjusted_eur": round(adjusted(best)),
            "engine": best.engine,
            "gearbox": best.gearbox,
            "power_kw": best.power_kw,
            "close_matches": len(close),
            **facts,
        }
    close_ids = {c.id for c, _ in close}
    nearest = sorted(
        chosen,
        key=lambda c: (
            c.id not in close_ids,
            abs(c.year - target.year),  # type: ignore[operator]
            abs((c.mileage_km or 0) - (target.mileage_km or 0)),
        ),
    )[: cfg.nearest_to_show]
    if benchmark and benchmark["id"] not in {c.id for c in nearest}:
        nearest = [close[0][0], *nearest][: cfg.nearest_to_show]
    return ValueEstimate(
        method="benchmark" if benchmark else "median",
        benchmark=benchmark,
        median_value_eur=round(median_value),
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
                "close": c.id in close_ids,
            }
            for c in nearest
        ),
    )
