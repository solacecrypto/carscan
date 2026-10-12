"""Currency hook. Phase 1 only sees EUR; SEK conversion plugs in here in phase 4."""

from decimal import ROUND_HALF_UP, Decimal


class UnsupportedCurrency(ValueError):
    pass


def to_eur(amount: Decimal | int | float, currency: str = "EUR") -> int:
    """Convert an amount to whole euros. Raises for currencies without a configured rate."""
    if currency.upper() != "EUR":
        raise UnsupportedCurrency(currency)
    return int(Decimal(str(amount)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
