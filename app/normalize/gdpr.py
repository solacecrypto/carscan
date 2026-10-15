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


_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def clean_text(text: str | None) -> str | None:
    """Drop lone UTF-16 surrogates (broken emoji from some sites); Postgres rejects them."""
    if not text:
        return text
    return _SURROGATE_RE.sub("", text)


def clean_json(obj: Any) -> Any:
    if isinstance(obj, str):
        return clean_text(obj)
    if isinstance(obj, dict):
        return {k: clean_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_json(v) for v in obj]
    return obj


def scrub_text(text: str | None) -> str | None:
    if not text:
        return text
    text = clean_text(text) or ""
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
