"""One structured Claude call per description, via the official SDK."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import anthropic
import structlog

from app.insights.schema import JSON_SCHEMA, SYSTEM_PROMPT, DescriptionInsight

log = structlog.get_logger()

MAX_DESCRIPTION_CHARS = 12_000


@dataclass(frozen=True)
class AnalysisResult:
    status: str  # ok | refused | error
    insight: DescriptionInsight | None = None
    raw: dict[str, Any] | None = None
    error: str | None = None
    model: str | None = None


Analyzer = Callable[[str, str], AnalysisResult]


def build_user_message(title: str, description: str) -> str:
    text = description.strip()
    if len(text) > MAX_DESCRIPTION_CHARS:
        text = text[:MAX_DESCRIPTION_CHARS] + "\n[...truncated]"
    return f"Car: {title}\n\nAdvertisement text:\n<description>\n{text}\n</description>"


class ClaudeAnalyzer:
    def __init__(self, api_key: str, model: str = "claude-opus-5-5") -> None:
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=60.0)
        self.model = model

    def __call__(self, title: str, description: str) -> AnalysisResult:
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=1024,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": JSON_SCHEMA},
                },
                system=[
                    {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
                ],
                messages=[{"role": "user", "content": build_user_message(title, description)}],
            )
        except anthropic.RateLimitError as e:
            return AnalysisResult("error", error=f"rate limited: {e.message}", model=self.model)
        except anthropic.APIStatusError as e:
            return AnalysisResult(
                "error", error=f"HTTP {e.status_code}: {e.message}", model=self.model
            )
        except anthropic.APIConnectionError as e:
            return AnalysisResult("error", error=f"connection: {e}", model=self.model)
        if response.stop_reason == "refusal":
            cat = response.stop_details.category if response.stop_details else None
            return AnalysisResult("refused", error=f"refused ({cat})", model=response.model)
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
            insight = DescriptionInsight.model_validate(data)
        except (ValueError, TypeError) as e:
            return AnalysisResult(
                "error",
                error=f"invalid JSON from model: {e}",
                raw={"text": text},
                model=response.model,
            )
        usage = response.usage
        log.info(
            "insights.call",
            model=response.model,
            input_tokens=usage.input_tokens,
            cached=getattr(usage, "cache_read_input_tokens", None),
            output_tokens=usage.output_tokens,
        )
        return AnalysisResult("ok", insight=insight, raw=data, model=response.model)
