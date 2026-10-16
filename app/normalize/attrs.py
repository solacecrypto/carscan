"""Canonical colour and body type across sites and languages (DE/NL/FR/IT/SV/EN)."""

from __future__ import annotations

import re

COLORS = (
    "black", "grey", "silver", "white", "blue", "red", "green", "brown", "beige",
    "yellow", "orange", "gold", "purple", "other",
)  # fmt: skip

_COLOR_WORDS = {
    "black": r"schwarz|zwart|noir|nero|svart|black",
    "grey": r"grau|grijs|gris|grigio|gr[åa]|anthrazit|antraciet|antracite|grey|gray|graphite",
    "silver": r"silber|zilver|argent|argento|silver",
    "white": r"wei[sß]|wit|blanc|bianco|vit|white",
    "blue": r"blau|blauw|bleu|blu|bl[åa]|blue",
    "red": r"rot|rood|rouge|rosso|r[öo]d|red|bordeaux",
    "green": r"gr[üu]n|groen|vert|verde|gr[öo]n|green",
    "brown": r"braun|bruin|brun|marrone|brown",
    "beige": r"beige|creme|cr[èe]me|champagne",
    "yellow": r"gelb|geel|jaune|giallo|gul|yellow",
    "orange": r"orange|oranje|arancio",
    "gold": r"gold|goud|oro|guld",
    "purple": r"violett|lila|paars|violet|viola|purple",
}
# substring match on purpose: sellers write "Saphirschwarz", "Alpinweiß", "Donkerblauw"
_COLOR_RE = {k: re.compile(rf"(?:{v})", re.I) for k, v in _COLOR_WORDS.items()}


def canonical_color(text: str | None) -> str | None:
    if not text:
        return None
    for key, rx in _COLOR_RE.items():
        if rx.search(text):
            return key
    return "other"


BODIES = ("sedan", "estate", "hatchback", "coupe", "convertible", "suv", "mpv")

_BODY_WORDS = (
    ("estate", r"estate|kombi|variant|avant|touring|t-modell|stationwagon|stationwagen|break|station|sw|familiare|allroad|alltrack|v50|v60|v70|v90|xc70"),
    ("convertible", r"cabrio|convertible|roadster|spider|spyder|cab\b"),
    ("coupe", r"coup[ée]|cc\b|gran coupe|sportcoup"),
    ("mpv", r"sportsvan|monovolume|mpv|van\b|touran|sharan|b-klasse|minivan|plus\b|people"),
    ("suv", r"suv|terreinwagen|gel[äa]ndewagen|fuoristrada|crossover|xc60|xc90|q[3578]\b|x[1-7]\b|tiguan|gla\b|glc\b"),
    ("hatchback", r"hatchback|schr[äa]gheck|limousine 5|3-t[üu]rer|5-t[üu]rer|3-deurs|5-deurs|stadsauto|sportback|crossgolf|berlina 3|berlina 5|halvkombi|kleinwagen|compact|citycar|standardmodell|standard model|kompakt"),
    ("sedan", r"sedan|limousine|limo|berline|berlina|saloon|stufenheck|s60|s80|s90"),
)  # fmt: skip
_BODY_RE = [(k, re.compile(v, re.I)) for k, v in _BODY_WORDS]


def canonical_body(text: str | None, hint_bodies: list[str] | None = None) -> str | None:
    """Map a site's body label to one of BODIES. ``hint_bodies`` are the watchlist model's
    bodies: a generic label ('standardmodell') on a sedan-only model means sedan."""
    if not text:
        return None
    t = text.strip().lower()
    if t in BODIES:
        return t
    for key, rx in _BODY_RE.items() if isinstance(_BODY_RE, dict) else _BODY_RE:
        if rx.search(t):
            if key == "hatchback" and hint_bodies and "hatchback" not in hint_bodies:
                return hint_bodies[0] if len(hint_bodies) == 1 else None
            return key
    if hint_bodies and len(hint_bodies) == 1:
        return hint_bodies[0]
    return None
