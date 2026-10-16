from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.paths import config_file

CONFIG_PATH = config_file("costs.yaml")


@dataclass(frozen=True)
class Tariff:
    year: int
    fixed: Decimal
    brackets: tuple[tuple[int | None, Decimal], ...]  # (upper g/km inclusive or None, €/g)
    source: str = ""


@dataclass(frozen=True)
class CostConfig:
    version: int
    transport: dict[str, Decimal]
    fixed_fees: dict[str, Decimal]
    zero_after_months: int
    depreciation_table: tuple[tuple[int, Decimal, Decimal], ...]
    tariffs: tuple[Tariff, ...]
    co2_fallback: dict[str, int] = field(default_factory=dict)
    valuation: dict[str, Decimal] = field(default_factory=dict)
    repairs: dict[str, tuple[int, int]] = field(default_factory=dict)  # key -> (low, high) €
    travel: dict[str, Any] = field(default_factory=dict)  # pickup trip parameters

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CostConfig:
        b = d["bpm"]
        return cls(
            version=int(d.get("version", 1)),
            transport={k.upper(): Decimal(str(v)) for k, v in d["transport"].items()},
            fixed_fees={k: Decimal(str(v)) for k, v in d["fixed_fees"].items()},
            zero_after_months=int(b["zero_after_months"]),
            depreciation_table=tuple(
                (int(m), Decimal(str(base)), Decimal(str(per)))
                for m, base, per in b["depreciation_table"]
            ),
            tariffs=tuple(
                Tariff(
                    year=int(t["year"]),
                    fixed=Decimal(str(t.get("fixed", 0))),
                    brackets=tuple(
                        (None if up is None else int(up), Decimal(str(rate)))
                        for up, rate in t["brackets"]
                    ),
                    source=str(t.get("source", "")),
                )
                for t in b["tariffs"]
            ),
            co2_fallback={str(k).lower(): int(v) for k, v in (b.get("co2_fallback") or {}).items()},
            valuation={k: Decimal(str(v)) for k, v in (d.get("valuation") or {}).items()},
            repairs={str(k): (int(v[0]), int(v[1])) for k, v in (d.get("repairs") or {}).items()},
            travel=dict(d.get("travel") or {}),
        )

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> CostConfig:
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(yaml.safe_load(f))


@lru_cache
def get_cost_config() -> CostConfig:
    return CostConfig.load()
