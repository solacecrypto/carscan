"""Rule-based condition tags from seller descriptions, in DE/NL/FR/IT/SV.

Cheap and instant; the Claude pass (phase 6) refines the same fields when a key is set."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

# polarity: good | bad | note
PATTERNS: list[tuple[str, str, str, str]] = [
    # (key, label, polarity, regex)
    (
        "chain_replaced",
        "chain replaced",
        "good",
        r"(steuerkette|catena( di distribuzione)?|distributieketting|ketting|kamkedja|cha[iî]ne de distribution).{0,45}?(neu\b|erneuert|gewechselt|getauscht|ersetzt|gemacht|sostituita|nuova|vervangen|nieuw|bytt|ny|remplac[ée]e?|neuve)"
        r"|(neue|nieuwe|ny)\W{0,3}(steuerkette|distributieketting|kamkedja)",
    ),
    (
        "belt_replaced",
        "timing belt replaced",
        "good",
        r"(zahnriemen|distributieriem|cinghia( di distribuzione)?|kamrem|courroie de distribution).{0,45}?(neu\b|erneuert|gewechselt|getauscht|ersetzt|sostituita|nuova|vervangen|nieuw|bytt|remplac[ée]e?|neuve)"
        r"|(neuer|nieuwe|ny)\W{0,3}(zahnriemen|distributieriem|kamrem)",
    ),
    (
        "accident_free",
        "accident-free",
        "good",
        r"\bunfallfrei|\bschadevrij|\bschadenvrij|senza incidenti|mai incidentata|non incidentata|\bkrockfri|\bolycksfri|jamais accident",
    ),
    (
        "accident",
        "accident",
        "bad",
        r"\bunfall(wagen|fahrzeug|schaden|auto)|unfall\b(?!frei)|hatte einen unfall|\bincidentat[ao]|\bincidente\b|\bsinistrat[ao]|\bschade(auto|wagen)\b|\bschadeverleden|\bkrockskad|accident[ée]e?\b|\bbastelauto|\bexportfahrzeug",
    ),
    (
        "engine_damage",
        "engine damage",
        "bad",
        r"motorschaden|motor defekt|motore (rotto|guasto|da rifare)|motorschade|motor kapot|motorfel|moteur (hs|cass[ée])",
    ),
    (
        "gearbox_damage",
        "gearbox damage",
        "bad",
        r"getriebeschaden|getriebe defekt|cambio (rotto|guasto|da rifare)|bak(schade|kapot)|versnellingsbak (kapot|defect)|växellåd[as]?fel|bo[iî]te (hs|cass[ée])",
    ),
    (
        "rust",
        "rust",
        "bad",
        r"\brost\b|\brostig|\broest\b|\bruggine\b|\brost(ig|angrepp)|\brouille",
    ),
    (
        "oil_consumption",
        "burns oil",
        "bad",
        r"öl\s?verbrauch|verbraucht öl|braucht öl|olie\s?verbruik|verbruikt olie|consumo (di )?olio|oljeförbrukning|drar olja|consomme de l.huile",
    ),
    (
        "warning_light",
        "warning light",
        "bad",
        r"motorkontroll?leuchte|kontrollleuchte|warnleuchte|spia (motore|accesa)|storingslamp|motorlamp|motorstörning|voyant (moteur|allum[ée])",
    ),
    (
        "papers_missing",
        "papers missing",
        "bad",
        r"ohne (fahrzeug)?brief|ohne papiere|ohne (fahrzeug)?schein|ohne zulassungsbescheinigung|ohne coc|(fahrzeug)?brief (fehlt|verloren|nicht vorhanden|abhanden)|papiere (fehlen|verloren|nicht vorhanden)|keine papiere|kein (fahrzeug)?brief|zonder papieren|zonder kentekenbewijs|papieren ontbreken|geen papieren|senza libretto|senza documenti|documenti mancanti|utan registreringsbevis|saknar (papper|registreringsbevis)|sans papiers|sans carte grise|no papers|without papers|missing (papers|paperwork|logbook)|no logbook|no v5",
    ),
    (
        "tuv_new",
        "TÜV / inspection new",
        "good",
        r"(t[üu]v|(?<![a-z])hu|hauptuntersuchung)\W{0,3}(bis|g[üu]ltig(?: bis)?|until|valid|neu bis|=|:)?\W{0,4}(neu|nieuw|new|\d{1,2}[./]\d{2,4}|20\d{2})|apk\W{0,8}(nieuw|tot|t/m|geldig)|revisione (fatta|nuova|appena|valida)|besiktigad|nybesiktigad|contr[ôo]le technique (ok|r[ée]cent|vierge|valable)",
    ),
    (
        "full_history",
        "full service history",
        "good",
        r"scheckheft|serviceheft( gepflegt)?|lückenlos|volledige onderhoudshistorie|onderhoudshistorie|onderhoudsboekje|dealer onderhouden|tagliandi (certificati|completi|regolari)|tagliandata|servicebok|fullservad|carnet d.entretien",
    ),
    (
        "export_only",
        "export / trade only",
        "bad",
        r"nur (für den )?export|nur an (gewerbe|händler)|händlerverkauf|export(fahrzeug|auto)?\b.{0,20}(nur|only)|alleen (voor )?export|solo (per )?export|solo commercianti|endast export|uniquement export",
    ),
    (
        "new_tyres",
        "new tyres",
        "note",
        r"neue reifen|reifen neu|nieuwe banden|gomme nuove|nya däck|pneus neufs",
    ),
    (
        "new_brakes",
        "new brakes",
        "note",
        r"bremsen neu|neue bremsen|nieuwe remmen|freni nuovi|nya bromsar|freins neufs",
    ),
    (
        "new_clutch",
        "new clutch",
        "note",
        r"kupplung neu|neue kupplung|nieuwe koppeling|frizione nuova|ny koppling|embrayage neuf",
    ),
    (
        "recent_service",
        "recently serviced",
        "note",
        r"frisch gewartet|inspektion neu|gerade gewartet|net (een )?(grote )?beurt|tagliando (appena )?fatto|nyservad|r[ée]vision (faite|r[ée]cente)",
    ),
    (
        "ac_broken",
        "air conditioning broken",
        "bad",
        r"klima(anlage)? (defekt|funktioniert nicht|ohne funktion)|airco (defect|werkt niet|kapot)|clima (non funziona|rotto)|ac (fungerar inte|trasig)|clim (hs|ne fonctionne)",
    ),
    (
        "needs_repair",
        "needs repair",
        "bad",
        r"reparaturbedürftig|muss repariert|defekt(e|es)? \w+|nicht fahrbereit|non marciante|da riparare|opknapper|moet gerepareerd|reparationsobjekt|renoveringsobjekt|à réparer",
    ),
    (
        "one_owner",
        "one owner",
        "note",
        r"erste hand|1\.? ?hand|erstbesitz|eerste eigenaar|1e eigenaar|unico proprietario|en ägare|première main",
    ),
]
_COMPILED = [(k, label, pol, re.compile(rx, re.I)) for k, label, pol, rx in PATTERNS]
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?\n])\s+|\s*\n+\s*")


@dataclass(frozen=True)
class Tag:
    key: str
    label: str
    polarity: str
    evidence: str
    until: str | None = None  # TÜV / inspection validity as "MM/YYYY" when the ad gives one

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_BOUNDARY = re.compile(r"[.!?](?=\s+[A-ZÄÖÜÅÉ(])|\n")


def _sentence_around(text: str, start: int, end: int) -> str:
    """The sentence containing [start, end); boundaries are '.', '!', '?' followed by a capital
    letter, or a line break, so '150.000 km' and '3.5' do not split a sentence."""
    left = 0
    for m in _BOUNDARY.finditer(text, 0, start):
        left = m.end()
    right = len(text)
    m = _BOUNDARY.search(text, end)
    if m:
        right = m.end()
    return re.sub(r"\s+", " ", text[left:right]).strip()[:220]


_VALID_UNTIL = re.compile(r"(\d{1,2})[./-](\d{2,4})|\b(20\d{2})\b")


def _with_validity(tag: Tag, text: str, pos: int) -> Tag:
    """'TÜV bis 06/2027', 'HU 03/28', 'TÜV neu bis 2028' → until='06/2027' and a dated label."""
    m = _VALID_UNTIL.search(text, pos, pos + 60)
    if not m:
        return tag
    if m.group(3):
        until = f"12/{m.group(3)}"
    else:
        month, year = int(m.group(1)), int(m.group(2))
        if not 1 <= month <= 12:
            return tag
        year = year + 2000 if year < 100 else year
        if not 2020 <= year <= 2040:
            return tag
        until = f"{month:02d}/{year}"
    return Tag(tag.key, f"TÜV / inspection until {until}", tag.polarity, tag.evidence, until)


def extract_tags(text: str | None) -> list[Tag]:
    if not text:
        return []
    tags: list[Tag] = []
    seen: set[str] = set()
    for key, label, pol, rx in _COMPILED:
        m = rx.search(text)
        if not m or key in seen:
            continue
        seen.add(key)
        tag = Tag(key, label, pol, _sentence_around(text, m.start(), m.end()))
        if key == "tuv_new":
            tag = _with_validity(tag, text, m.start())
        tags.append(tag)
    # "accident" never wins over an explicit "accident-free" claim about the same text
    if "accident_free" in seen and "accident" in seen:
        tags = [t for t in tags if t.key != "accident"]
    order = {"bad": 0, "good": 1, "note": 2}
    return sorted(tags, key=lambda t: order[t.polarity])


def summarize(tags: list[Tag]) -> dict[str, Any]:
    """Map tags onto the structured fields the Claude pass also fills."""
    keys = {t.key for t in tags}
    return {
        "accident": "yes"
        if "accident" in keys
        else ("no" if "accident_free" in keys else "unknown"),
        "accident_evidence": next(
            (t.evidence for t in tags if t.key in ("accident", "accident_free")), None
        ),
        "timing_chain": "replaced" if "chain_replaced" in keys else "not_mentioned",
        "service_history": "full" if "full_history" in keys else "unknown",
        "export_only": "export_only" in keys,
        "defects": [
            t.label
            for t in tags
            if t.polarity == "bad" and t.key not in ("accident", "export_only")
        ],
    }
