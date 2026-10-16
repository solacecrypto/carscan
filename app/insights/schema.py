"""What we ask Claude to extract from a seller's description."""

from typing import Literal

from pydantic import BaseModel, Field


class DescriptionInsight(BaseModel):
    language: str = Field(
        description="ISO 639-1 code of the description's language, e.g. de, it, nl"
    )
    summary: str = Field(description="Two short English sentences a buyer would want to hear first")
    accident: Literal["yes", "no", "unknown"] = Field(
        description=(
            "yes if the text admits accident/crash damage (repaired or not); "
            "no only if it explicitly states accident-free"
        )
    )
    accident_evidence: str = Field(
        description="Quote (original language) that supports the accident verdict, or empty"
    )
    timing_chain: Literal["replaced", "due", "not_mentioned"] = Field(
        description=(
            "replaced if the timing chain/belt (Steuerkette, catena di distribuzione, "
            "distributieketting, kamkedja) was replaced"
        )
    )
    service_history: Literal["full", "partial", "none", "unknown"] = Field(
        description=(
            "full = complete service history / Scheckheft gepflegt / tagliandi completi; "
            "partial = some records; none = explicitly none"
        )
    )
    export_only: bool = Field(
        description="true if sold only for export or to traders ('nur Export/Händler')"
    )
    defects: list[str] = Field(
        description=(
            "Short English phrases for each admitted defect, warning light, rust, noise, "
            "or needed repair; empty if none"
        )
    )
    photo_flags: list[str] = Field(
        default_factory=list,
        description=(
            "What the photos show that a buyer should know: visible rust, dents, mismatched "
            "paint or panels, warning lights on the dashboard, winter tyres, worn interior, "
            "missing trim. Short English phrases; empty when the photos look fine or none given"
        ),
    )


JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "language": {"type": "string"},
        "summary": {"type": "string"},
        "accident": {"type": "string", "enum": ["yes", "no", "unknown"]},
        "accident_evidence": {"type": "string"},
        "timing_chain": {"type": "string", "enum": ["replaced", "due", "not_mentioned"]},
        "service_history": {"type": "string", "enum": ["full", "partial", "none", "unknown"]},
        "export_only": {"type": "boolean"},
        "defects": {"type": "array", "items": {"type": "string"}},
        "photo_flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "language",
        "summary",
        "accident",
        "accident_evidence",
        "timing_chain",
        "service_history",
        "export_only",
        "defects",
        "photo_flags",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You read used-car advertisements from German, Dutch, Belgian, Italian and Swedish
marketplaces for a small team that imports cars into the Netherlands. The text can be in any
language, often with dealer boilerplate. Extract only what the seller actually states; never
guess. "Unfallfrei", "schadevrij", "incidentata", "krockskadad" and similar words decide the
accident field. A replaced timing chain (Steuerkette erneuert, catena sostituita,
distributieketting vervangen, kamkedja bytt) is valuable: report it only when the text says it
was done, not when it is merely recommended. Mark export_only when the car is sold only for
export, only to traders/dealers, or explicitly without warranty for private buyers. List every
admitted defect, noise, warning light, rust spot or needed repair as a short English phrase.
When photos are attached, look at them like a careful buyer: report visible rust, dents,
scratches, mismatched paint or panel gaps, a lit warning light on the dashboard, winter tyres,
heavy interior wear or missing trim in photo_flags. Do not repeat what the text already says,
and do not guess from blurry images. Answer in the JSON schema you are given."""
