"""Strip personal data of private sellers before anything is stored (also from the raw blob)."""

import re
from typing import Any

# A phone number: 8+ digits with optional separators, not a price/mileage/power figure.
PHONE_RE = re.compile(
    r"(?<![\d.,])(\+?\d[\d \-/().]{6,}\d)(?![\d.,]*\s*(?:€|eur|km|kr|mil|cv|kw|hk|pk|ps|cc|cm)\b)",
    re.I,
)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _phone_sub(m: re.Match[str]) -> str:
    return "[removed]" if sum(ch.isdigit() for ch in m.group(1)) >= 8 else m.group(1)


# keys removed for every seller type (a dealer's contact person is a person too)
ALWAYS_DROP = {"phones", "contactName", "whatsappNumber", "phone", "email", "licensePlate"}
# keys removed only for private sellers (a dealer's address is business data)
PRIVATE_DROP = {"street", "latitude", "longitude", "name", "companyName"}


def scrub_text(text: str | None) -> str | None:
    if not text:
        return text
    text = PHONE_RE.sub(_phone_sub, text)
    return EMAIL_RE.sub("[removed]", text)


def scrub(obj: Any, *, private: bool) -> Any:
    """Return a deep copy of `obj` with personal-data keys removed."""
    if isinstance(obj, dict):
        drop = ALWAYS_DROP | (PRIVATE_DROP if private else set())
        return {k: scrub(v, private=private) for k, v in obj.items() if k not in drop}
    if isinstance(obj, list):
        return [scrub(v, private=private) for v in obj]
    return obj
