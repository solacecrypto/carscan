"""Currency hook. Rates for non-EUR sources live under `fx:` in costs.yaml."""

from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache

import yaml

from app.paths import config_file

_COSTS_YAML = config_file("costs.yaml")


class UnsupportedCurrency(ValueError):
    pass


@lru_cache
def rates() -> dict[str, Decimal]:
    """Units of foreign currency per euro, e.g. {"SEK": 11.2}."""
    try:
        with open(_COSTS_YAML, encoding="utf-8") as f:
            raw = (yaml.safe_load(f) or {}).get("fx") or {}
    except FileNotFoundError:
        raw = {}
    return {str(k).upper(): Decimal(str(v)) for k, v in raw.items()}


def to_eur(amount: Decimal | int | float, currency: str = "EUR") -> int:
    """Convert an amount to whole euros. Raises for currencies without a configured rate."""
    cur = currency.upper()
    value = Decimal(str(amount))
    if cur != "EUR":
        rate = rates().get(cur)
        if not rate:
            raise UnsupportedCurrency(currency)
        value = value / rate
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
