"""Rough repair-cost estimate from what the seller admits is wrong.

A multilingual pattern list (DE/NL/FR/IT/SV/EN) maps defects mentioned in the ad to a typical
NL garage price range for a ten-year-old car. The midpoint is added to the landed cost. This
is deliberately coarse: it exists to stop a cheap "needs work" car from looking like a deal,
not to replace a quote. Ranges live under ``repairs:`` in costs.yaml.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_DEFECT = r"(?:defekt|kaputt|schaden|undicht|fällig|verschlissen|muss|defect|kapot|lek|versleten|moet|rott[ao]|da sostituire|non funziona|trasig|läcker|slut|broken|failed|fault\w*|worn|needs?|leak\w*|not working|slipping|noise|due)"


def _near(subject: str, words: str = _DEFECT) -> str:
    """subject followed within three words by a defect word."""
    return rf"\b(?:{subject})\w*\W+(?:\w+\W+){{0,3}}(?:{words})"


@dataclass(frozen=True)
class RepairItem:
    key: str
    label: str
    low: int
    high: int
    pattern: str
    negatable: bool = True


ITEMS: tuple[RepairItem, ...] = (
    RepairItem("not_drivable", "not drivable: needs a trailer or tow", 200, 400,
        r"nicht fahrbereit|nicht fahrf[äa]hig|abgeschleppt|abschlepp|anh[äa]nger abgeholt|niet rijdend|niet rijklaar|non marciante|carro attrezzi|ej k[öo]rbar|b[äa]rgning|not drivable|not driveable|tow truck|on a trailer|by trailer", negatable=False),
    RepairItem("engine_damage", "engine damage", 2500, 5000,
        r"motorschaden|motor defekt|motorschade|motore rotto|motorfel|motorhaveri|engine (?:damage|failure|blown|broken)|blown engine"),
    RepairItem("injectors", "injectors", 600, 1500,
        r"injektor|einspritzd[üu]se|injector|iniettor|insprutning|verstuiver"),
    RepairItem("turbo", "turbo", 1000, 2000, _near("turbo")),
    RepairItem("gearbox", "gearbox", 1500, 3500,
        r"getriebeschaden|" + _near("getriebe|versnellingsbak|cambio|v[äa]xell[åa]d|gearbox|transmission")),
    RepairItem("clutch", "clutch", 700, 1200,
        _near("kupplung|koppeling|frizione|koppling|clutch", _DEFECT + r"|rutscht|slipt|slirar|gone")),
    RepairItem("timing_chain_due", "timing chain due", 1200, 2200,
        _near("steuerkette|distributieketting|catena|kamkedj|timing chain", _DEFECT + r"|rasselt|gel[äa]ngt|rammelt|rasslar|rattl\w*|stretched")),
    RepairItem("timing_belt_due", "timing belt due", 500, 900,
        _near("zahnriemen|distributieriem|cinghia|kamrem|timing belt", _DEFECT + r"|[üu]berf[äa]llig|aan vervanging toe|da fare|overdue")),
    RepairItem("head_gasket", "head gasket", 1200, 2500,
        r"zylinderkopfdichtung|koppakking|guarnizione (?:della )?testata|topplockspackning|head gasket"),
    RepairItem("dpf_egr", "particulate filter / EGR", 500, 1500,
        _near("dpf|partikelfilter|roetfilter|agr|egr|fap", _DEFECT + r"|voll|verstopft|verstopt|intasato|igensatt|blocked|clogged|warning")),
    RepairItem("warning_light", "warning light on", 150, 800,
        r"motorkontrollleuchte|\bmkl\b|fehlerspeicher|kontrollleuchte|storingslamp|motorlampje|spia (?:motore )?accesa|motorlampa|check engine|warning light|engine light"),
    RepairItem("ac_broken", "air conditioning", 300, 900,
        _near("klima|klimaanlage|airco|aria condizionata|\bac\b|a/c|air ?con", _DEFECT + r"|funktioniert nicht|ohne funktion|werkt niet|fungerar inte|k[üu]hlt nicht|koelt niet")),
    RepairItem("brakes", "brakes", 300, 700,
        _near("bremsen|bremsscheiben|remmen|remschijven|freni|bromsar|brakes|brake discs", _DEFECT + r"|neu machen|aan vervanging|da fare")),
    RepairItem("suspension", "suspension / steering parts", 300, 900,
        r"sto[ßs]d[äa]mpfer|querlenker|spurstange|traggelenk|schokdemper|draagarm|fuseekogel|ammortizzator|st[öo]td[äa]mpare|shock absorber|control arm|ball joint|" + _near("fahrwerk|suspension|ophanging")),
    RepairItem("steering", "power steering", 400, 1000,
        _near("servolenkung|lenkung|stuurbekrachtiging|servosterzo|servostyrning|power steering|steering", _DEFECT + r"|schwerg[äa]ngig|heavy|non funziona")),
    RepairItem("rust", "rust", 300, 1500,
        r"\brost\b|rostig|durchrostung|rostl[öo]cher|\broest\b|roestplekken|doorgeroest|ruggine|rostskador|rostangrepp|\brust(?:y|ed)?\b|corrosion"),
    RepairItem("body_damage", "bodywork damage", 300, 2000,
        r"unfallschaden|hagelschaden|blechschaden|karosserieschaden|beulen?\b|kratzer|schade aan|deuk|krassen|plaatschade|carrozzeria danneggiata|ammaccat|bucklor|pl[åa]tskador|\bdents?\b|scratches|body damage|bumper damage|accident damage"),
    RepairItem("airbag", "airbag fault", 300, 800,
        r"airbag\w* (?:leuchtet|defekt|lampe|leuchte)|airbaglamp|spia airbag|airbag(?:lampa| ?varning)|airbag (?:light|warning|fault)"),
    RepairItem("exhaust", "exhaust", 200, 600,
        _near("auspuff|uitlaat|scarico|avgassystem|exhaust", _DEFECT + r"|rusted")),
    RepairItem("electrical", "electrical fault", 150, 600,
        r"elektrik(?:problem|fehler)|elektrische? (?:storing|probleem)|problema elettrico|\belfel\b|electrical (?:fault|issue|problem)|fensterheber defekt|raammechanisme defect|window regulator (?:broken|faulty|not working|defect)"),
    RepairItem("battery", "battery", 100, 250,
        _near("batterie|accu|batteria|batteri|battery", _DEFECT + r"|leer|schwach|leeg|scarica|d[åa]ligt|dead|flat")),
    RepairItem("tyres_due", "tyres", 300, 600,
        _near("reifen|banden|gomme|d[äa]ck|tyres|tires", _DEFECT + r"|abgefahren|consumate|usurate|slitna|bald")),
    RepairItem("tuv_expired", "inspection expired", 150, 400,
        r"t[üu]v (?:abgelaufen|f[äa]llig|[üu]berf[äa]llig)|ohne t[üu]v|kein t[üu]v|apk (?:verlopen|is verlopen)|geen apk|zonder apk|revisione scaduta|senza revisione|besiktning(?:en)? (?:utg[åa]tt|ej godk[äa]nd)|no mot\b|mot expired|inspection expired", negatable=False),
    RepairItem("oil_consumption", "oil consumption", 300, 2000,
        r"[öo]l\s?verbrauch|verbraucht [öo]l|braucht [öo]l|olie\s?verbruik|verbruikt olie|consumo (?:di )?olio|oljef[öo]rbrukning|burns oil|oil consumption|uses oil"),
    RepairItem("oil_leak", "oil leak", 200, 800,
        r"[öo]lverlust|verliert [öo]l|[öo]lundicht|olielek|olie ?lekkage|verliest olie|perde olio|oljel[äa]ck|oil leak|leaks oil|leaking oil"),
    RepairItem("unspecified", "unspecified defects (seller says it needs work)", 500, 1500,
        r"bastler|reparaturbed[üu]rftig|reparatie nodig|opknapper|da riparare|da sistemare|renoveringsobjekt|reparationsobjekt|needs (?:repair|work|repairs)|project car|spares or repair|spare parts donor|hobbyist", negatable=False),
)  # fmt: skip

_NEGATION = re.compile(
    r"(?:\bkein\w*|\bohne|\bgeen|\bzonder|\bno\b|\bsenza|\binga\b|\bingen\b|\bnot\b|\bwithout|\bnicht|\bniet)\W+(?:\w+\W+){0,2}$"
)


def _sentence(text: str, start: int, end: int) -> str:
    left = max(text.rfind(".", 0, start), text.rfind("\n", 0, start), text.rfind("!", 0, start)) + 1
    right = min(
        [i for i in (text.find(".", end), text.find("\n", end), text.find("!", end)) if i != -1]
        or [len(text)]
    )
    return re.sub(r"\s+", " ", text[left:right]).strip()[:220]


def find_items(text: str) -> dict[str, str]:
    """Repair item key -> evidence sentence for everything the text admits."""
    found: dict[str, str] = {}
    low = text.lower()
    for item in ITEMS:
        for m in re.finditer(item.pattern, low):
            if item.negatable and _NEGATION.search(low[max(0, m.start() - 30) : m.start()]):
                continue
            found[item.key] = _sentence(text, m.start(), m.end())
            break
    return found


def estimate_repairs(
    *texts: str | None, ranges: dict[str, tuple[int, int]] | None = None
) -> dict[str, Any] | None:
    """Combine the original and the English text; specific items win over 'unspecified'."""
    found: dict[str, str] = {}
    for t in texts:
        if t:
            for k, ev in find_items(t).items():
                found.setdefault(k, ev)
    if len(found) > 1:
        found.pop("unspecified", None)
    if not found:
        return None
    by_key = {i.key: i for i in ITEMS}
    items = []
    for key, ev in found.items():
        it = by_key[key]
        lo, hi = (ranges or {}).get(key, (it.low, it.high))
        items.append({"key": key, "label": it.label, "low": lo, "high": hi, "evidence": ev})
    low, high = sum(i["low"] for i in items), sum(i["high"] for i in items)
    return {"items": items, "low": low, "high": high, "estimate": round((low + high) / 2)}
