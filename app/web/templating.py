from datetime import UTC, datetime
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import Undefined

from app.web.deps import csrf_token

STATIC_DIR = Path(__file__).parent / "static"
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


def _unread(user: object) -> int | None:
    """Unread notifications for the bell in the nav; one cheap count per full page."""
    if user is None:
        return None
    from app.db import db_session
    from app.notify import unread_count

    try:
        with db_session() as s:
            return unread_count(s, user.id)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - the bell must never break a page
        return None


_HEALTH_CACHE: dict[str, object] = {"at": None, "issues": []}


def _health_issues() -> list[object]:
    """Health banner for every full page; recomputed at most once a minute per process."""
    from app.db import db_session
    from app.health import check

    now = datetime.now(UTC)
    at = _HEALTH_CACHE["at"]
    if at is not None and (now - at).total_seconds() < 60:  # type: ignore[operator]
        return _HEALTH_CACHE["issues"]  # type: ignore[return-value]
    try:
        with db_session() as s:
            issues = list(check(s, now))
    except Exception:  # noqa: BLE001 - the banner must never break a page
        issues = []
    _HEALTH_CACHE["at"], _HEALTH_CACHE["issues"] = now, issues
    return issues


def _context(request: Request) -> dict[str, object]:
    user = getattr(request.state, "user", None)
    htmx = bool(request.headers.get("HX-Request"))
    ctx: dict[str, object] = {"csrf_token": csrf_token(request), "user": user, "is_htmx": htmx}
    if user is not None and not htmx:
        ctx["unread_notifications"] = _unread(user)
        ctx["health_issues"] = _health_issues()
    return ctx


templates = Jinja2Templates(directory=TEMPLATES_DIR, context_processors=[_context])
templates.env.filters.update({"eur": eur, "km": km, "month": month, "timeago": timeago})
