from datetime import UTC, datetime
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import Undefined

from app.web.deps import csrf_token

TEMPLATES_DIR = Path(__file__).parent / "templates"


def eur(value: int | float | None | Undefined) -> str:
    if value is None or isinstance(value, Undefined):
        return "–"
    return f"€ {value:,.0f}".replace(",", ".")


def km(value: int | float | None | Undefined) -> str:
    if value is None or isinstance(value, Undefined):
        return "–"
    return f"{value:,.0f} km".replace(",", ".")


def month(value: datetime | None) -> str:
    return "–" if value is None else value.strftime("%m/%Y")


def timeago(value: datetime | None) -> str:
    if value is None:
        return "never"
    delta = datetime.now(UTC) - value
    s = int(delta.total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def _context(request: Request) -> dict[str, object]:
    return {
        "csrf_token": csrf_token(request),
        "user": getattr(request.state, "user", None),
        "is_htmx": bool(request.headers.get("HX-Request")),
    }


templates = Jinja2Templates(directory=TEMPLATES_DIR, context_processors=[_context])
templates.env.filters.update({"eur": eur, "km": km, "month": month, "timeago": timeago})
