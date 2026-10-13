from app.insights.analyzer import AnalysisResult, Analyzer, ClaudeAnalyzer
from app.insights.schema import DescriptionInsight
from app.insights.service import analyze_pending, pending_listings, store_result

__all__ = [
    "AnalysisResult",
    "Analyzer",
    "ClaudeAnalyzer",
    "DescriptionInsight",
    "analyze_pending",
    "pending_listings",
    "store_result",
]


def default_analyzer() -> Analyzer | None:
    """The configured Claude analyzer, or None when no API key is set."""
    from app.config import get_settings

    s = get_settings()
    if not s.insights_enabled:
        return None
    return ClaudeAnalyzer(api_key=s.anthropic_api_key.get_secret_value(), model=s.claude_model)
