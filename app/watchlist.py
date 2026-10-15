"""Watchlist (models.yaml) -> saved searches. One model entry fans out to every source."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import SavedSearch, User

MODELS_PATH = Path(__file__).resolve().parents[1] / "models.yaml"

AS24_FUEL = {"petrol": "B", "diesel": "D", "electric": "E", "hybrid": "2", "lpg": "L"}
AS24_BODY = {
    "sedan": "6",
    "estate": "5",
    "coupe": "3",
    "convertible": "2",
    "suv": "4",
    "hatchback": "1",
    "van": "12",
}
MP_FUEL = {
    "petrol": "Benzine",
    "diesel": "Diesel",
    "electric": "Elektrisch",
    "hybrid": "Hybride",
    "lpg": "LPG",
}
MP_BODY = {
    "sedan": ["Sedan", "Berline"],
    "estate": ["Stationwagon", "Break"],
    "coupe": ["Coupé"],
    "convertible": ["Cabriolet"],
    "suv": ["SUV of Terreinwagen"],
    "hatchback": ["Hatchback (3/5-deurs)"],
    "van": ["MPV"],
}
BYTBIL_FUEL = {"petrol": "Bensin", "diesel": "Diesel", "electric": "El", "hybrid": "Hybrid"}
DEFAULT_INTERVALS = {
    "autoscout24_de": 30,
    "autoscout24_be": 60,
    "autoscout24_it": 60,
    "tweedehands_be": 60,
    "kleinanzeigen": 60,
    "automobile_it": 360,
    "bytbil": 360,
    "subito": 120,
    "autoscout24_nl": 360,
    "marktplaats": 360,
    "gaspedaal": 360,
}


@dataclass(frozen=True)
class SearchSpec:
    name: str
    source: str
    role: str
    params: dict[str, Any]
    interval_minutes: int


def load_models(path: Path = MODELS_PATH) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return list(data.get("models") or [])


def _motor_regex(motors: list[str], spaced: bool = False) -> str:
    if spaced:  # Bytbil writes "320 i"
        parts = [re.escape(m[:-1]) + r"\s?" + re.escape(m[-1:]) for m in motors]
    else:
        parts = [re.escape(m) for m in motors]
    alts = "|".join(parts)
    return rf"\b{alts}\b" if len(parts) == 1 else rf"\b({alts})\b"


def build_specs(model: dict[str, Any]) -> list[SearchSpec]:
    """Translate one watchlist entry into search specs for each configured source."""
    label, make = model["label"], model["make"]
    y0, y1 = model["years"]
    fuel = model.get("fuel", "petrol")
    motors: list[str] = model.get("motors") or []
    bodies: list[str] = model.get("bodies") or []
    as24 = model.get("autoscout24") or {}
    mp = model.get("marktplaats") or {}
    ka = model.get("kleinanzeigen") or {}
    am = model.get("automobile_it") or {}
    bb = model.get("bytbil") or {}
    srcs = model.get("sources") or {}
    specs: list[SearchSpec] = []

    def add(source: str, role: str, params: dict[str, Any], suffix: str = "") -> None:
        name = f"{label} · {source}{(' · ' + suffix) if suffix else ''}"
        specs.append(SearchSpec(name, source, role, params, DEFAULT_INTERVALS.get(source, 60)))

    as24_common = {
        "fuel": AS24_FUEL.get(fuel, "B"),
        "fregfrom": y0,
        "fregto": y1,
        "body": ",".join(sorted(AS24_BODY[b] for b in bodies if b in AS24_BODY)) or None,
    }
    as24_common = {k: v for k, v in as24_common.items() if v is not None}
    for role, sources in (
        ("deals", srcs.get("deals") or []),
        ("comparables", srcs.get("comparables") or []),
    ):
        for source in sources:
            if source in ("autoscout24_de", "autoscout24_nl", "autoscout24_be") and as24.get("cat"):
                cy = {"autoscout24_de": "D", "autoscout24_nl": "NL", "autoscout24_be": "B"}[source]
                path = as24["path"] if source != "autoscout24_be" else "/nl" + as24["path"]
                # the model path ORs a model-line filter in; the make-only path keeps `cat` exact
                path = path.rsplit("/", 1)[0] if source != "autoscout24_de" else path
                add(source, role, {"path": path, "cat": as24["cat"], "cy": cy, **as24_common})
            elif source == "autoscout24_it" and as24.get("it_path"):
                add(
                    source,
                    role,
                    {
                        "path": as24["it_path"],
                        "cy": "I",
                        "models": as24.get("it_models") or [],
                        **as24_common,
                    },
                )
            elif source in ("marktplaats", "tweedehands_be") and mp.get("make_id"):
                # Belgian 2dehands labels bodies in French as well as Dutch
                labels = (
                    MP_BODY
                    if source == "tweedehands_be"
                    else {k: v[:1] for k, v in MP_BODY.items()}
                )
                mp_bodies = [x for b in bodies for x in labels.get(b, [])]
                for motor in motors or [""]:
                    add(
                        source,
                        role,
                        {
                            "l2CategoryId": mp["make_id"],
                            "model": mp["model"],
                            "fuel": MP_FUEL.get(fuel, "Benzine"),
                            "year_from": y0,
                            "year_to": y1,
                            "query": motor or mp["model"],
                            "motor_regex": _motor_regex([motor]) if motor else None,
                            "bodies": mp_bodies,
                        }
                        | ({} if motor else {}),
                        suffix=motor,
                    )
            elif source == "kleinanzeigen" and ka.get("make"):
                for motor in motors or [make.lower()]:
                    add(
                        source,
                        role,
                        {
                            "make": ka["make"],
                            "query": motor,
                            "ez_from": y0,
                            "ez_to": y1,
                            "motor_regex": _motor_regex([motor]) if motors else None,
                        },
                        suffix=motor,
                    )
            elif source == "automobile_it" and am.get("path"):
                add(
                    source,
                    role,
                    {
                        "path": am["path"],
                        "attr_m_year_min": y0,
                        "attr_m_year_max": y1,
                        "fuel": {"petrol": "benzina", "diesel": "diesel"}.get(fuel, ""),
                        "motor_regex": _motor_regex(motors) if motors else None,
                    },
                )
            elif source == "bytbil" and bb.get("make"):
                add(
                    source,
                    role,
                    {
                        "Makes": bb["make"],
                        "Models": bb["model"],
                        "ModelYearRange.From": y0,
                        "ModelYearRange.To": y1,
                        "Fuels": BYTBIL_FUEL.get(fuel, "Bensin"),
                        "motor_regex": _motor_regex(motors, spaced=True) if motors else None,
                    },
                )
            elif source == "gaspedaal" and model.get("gaspedaal"):
                g = model["gaspedaal"]
                add(
                    source,
                    role,
                    {
                        "path": g["path"],
                        "bmin": y0,
                        "bmax": y1,
                        "motor_regex": _motor_regex(motors) if motors else None,
                        "bodies": [
                            x
                            for b in bodies
                            for x in {"sedan": ["Sedan"], "estate": ["Stationwagon"]}.get(b, [])
                        ],
                    },
                )
    for sp in specs:
        for k in [k for k, v in sp.params.items() if v is None]:
            del sp.params[k]
    return specs


def _canon(params: dict[str, Any]) -> str:
    import json

    def norm(v: Any) -> Any:
        if isinstance(v, list):
            return sorted(str(x) for x in v)
        return str(v) if isinstance(v, int | float) else v

    return json.dumps({k: norm(v) for k, v in (params or {}).items()}, sort_keys=True)


def sync(
    session: Session, owner: User, models: list[dict[str, Any]], prune: bool = False
) -> dict[str, int]:
    """Create missing searches per model; optionally pause watchlist searches no longer listed.

    A hand-made search with the same source and parameters is adopted (renamed) instead of
    duplicated, so the scrapers never run the same query twice."""
    wanted = {spec.name: spec for m in models for spec in build_specs(m)}
    rows = list(session.scalars(select(SavedSearch)))
    existing = {s.name: s for s in rows}
    by_params = {(s.source, _canon(s.source_params)): s for s in rows}
    created = updated = paused = 0
    for name, spec in wanted.items():
        row = existing.get(name)
        if row is None:
            twin = by_params.get((spec.source, _canon(spec.params)))
            if twin is not None and twin.name not in wanted:
                twin.name = name
                existing[name] = twin
                updated += 1
                continue
        if row is None:
            session.add(
                SavedSearch(
                    name=name,
                    owner=owner,
                    source=spec.source,
                    role=spec.role,
                    source_params=spec.params,
                    filters={},
                    interval_minutes=spec.interval_minutes,
                    alerts_enabled=spec.role == "deals",
                    active=True,
                )
            )
            created += 1
        elif row.source_params != spec.params or not row.active:
            row.source_params, row.active = spec.params, True
            updated += 1
    if prune:
        for row in rows:
            if " · " in row.name and row.name not in wanted and row.active:
                row.active = False
                paused += 1
    session.flush()
    return {"created": created, "updated": updated, "paused": paused}
