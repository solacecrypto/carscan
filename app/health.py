"""Is the machine still running? Every worker job records a heartbeat; a few rules turn the
heartbeats and the run history into issues that show as a banner on the site, as a
notification to every user, and as an admin Telegram message when one first appears."""

from __future__ import annotations

import functools
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ParamSpec, TypeVar

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.base import db_session
from app.db.models import (
    Digest,
    Heartbeat,
    Listing,
    ModelHistory,
    SavedSearch,
    ScrapeRun,
    ScrapeStatus,
)

log = structlog.get_logger()
P = ParamSpec("P")
R = TypeVar("R")


@dataclass(frozen=True)
class Issue:
    key: str
    severity: str  # "error" | "warning"
    title: str
    detail: str


def beat(
    job: str, ok: bool, error: str | None = None, detail: dict[str, Any] | None = None
) -> None:
    now = datetime.now(UTC)
    with db_session() as s:
        row = s.get(Heartbeat, job)
        if row is None:
            row = Heartbeat(job=job)
            s.add(row)
        row.last_run_at = now
        if ok:
            row.last_ok_at = now
            row.last_error = None
        else:
            row.last_error = (error or "failed")[:2000]
        if detail is not None:
            row.detail = detail


def with_heartbeat(job: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Wrap a scheduler job so its success or failure is recorded, whatever it returns."""

    def deco(fn: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                result = fn(*args, **kwargs)
            except Exception as e:
                beat(job, False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}")
                raise
            detail = result if isinstance(result, dict) else None
            beat(job, True, detail=detail)
            return result

        return wrapper

    return deco


def _ago(ts: datetime | None, now: datetime) -> str:
    if ts is None:
        return "never"
    m = int((now - ts).total_seconds() // 60)
    return f"{m} min ago" if m < 120 else f"{m // 60} h ago"


def check(session: Session, now: datetime | None = None) -> list[Issue]:
    now = now or datetime.now(UTC)
    beats = {h.job: h for h in session.scalars(select(Heartbeat))}
    issues: list[Issue] = []

    # 1. the scrape loop itself
    active = (
        session.scalar(
            select(func.count()).select_from(SavedSearch).where(SavedSearch.active.is_(True))
        )
        or 0
    )
    scrape = beats.get("scrape_due")
    if active:
        if (
            scrape is None
            or scrape.last_run_at is None
            or now - scrape.last_run_at > timedelta(minutes=20)
        ):
            issues.append(
                Issue(
                    "scrape_loop",
                    "error",
                    "The scrape loop is not running",
                    f"last tick {_ago(scrape.last_run_at if scrape else None, now)}; "
                    "the worker may be down or crashing",
                )
            )
        elif scrape.last_ok_at is None or now - scrape.last_ok_at > timedelta(minutes=30):
            issues.append(
                Issue(
                    "scrape_loop",
                    "error",
                    "The scrape loop keeps failing",
                    (scrape.last_error or "")[:300],
                )
            )
        last_ok_run = session.scalar(
            select(func.max(ScrapeRun.finished_at)).where(ScrapeRun.status == ScrapeStatus.ok)
        )
        if last_ok_run and now - last_ok_run > timedelta(minutes=45):
            issues.append(
                Issue(
                    "no_runs",
                    "error",
                    "No search has succeeded for a while",
                    f"last successful run {_ago(last_ok_run, now)}",
                )
            )

    # 2. per source: the canary. A site whose last 3 runs all failed, or with no success in
    # 3 h while it has active searches, is blocked or its page layout changed.
    sources = session.execute(
        select(SavedSearch.source, func.count())
        .where(SavedSearch.active.is_(True))
        .group_by(SavedSearch.source)
    ).all()
    for source, _n in sources:
        recent = list(
            session.scalars(
                select(ScrapeRun)
                .join(SavedSearch)
                .where(SavedSearch.source == source, ScrapeRun.finished_at.isnot(None))
                .order_by(ScrapeRun.started_at.desc())
                .limit(3)
            )
        )
        last_ok = session.scalar(
            select(func.max(ScrapeRun.finished_at))
            .join(SavedSearch)
            .where(SavedSearch.source == source, ScrapeRun.status == ScrapeStatus.ok)
        )
        if len(recent) == 3 and all(r.status is ScrapeStatus.failed for r in recent):
            err = recent[0].error or ""
            kind = "blocked" if "Blocked" in err or "403" in err else "failing"
            issues.append(
                Issue(
                    f"source:{source}",
                    "error",
                    f"{source} is {kind}",
                    f"last 3 runs failed · {err[:160]}",
                )
            )
        elif last_ok and now - last_ok > timedelta(hours=3):
            issues.append(
                Issue(
                    f"source:{source}",
                    "warning",
                    f"{source}: no successful run for {_ago(last_ok, now)}",
                    "its searches may be stuck or its page layout may have changed",
                )
            )

    # 3. background jobs
    pending_tr = (
        session.scalar(
            select(func.count())
            .select_from(Listing)
            .where(
                Listing.is_active.is_(True),
                Listing.description.isnot(None),
                Listing.description != "",
                Listing.description_en.is_(None),
            )
        )
        or 0
    )
    tr = beats.get("translate")
    if (
        pending_tr > 200
        and tr is not None
        and tr.last_ok_at
        and now - tr.last_ok_at > timedelta(hours=6)
    ):
        issues.append(
            Issue(
                "translate",
                "warning",
                "Translation has not progressed for 6 hours",
                f"{pending_tr} descriptions waiting; the provider may be blocking us",
            )
        )
    for job, label, hours in (
        ("geocode", "Geocoding", 6),
        ("enrich", "Ad-page catch-up", 6),
    ):
        b = beats.get(job)
        if (
            b is not None
            and b.last_error
            and (b.last_ok_at is None or now - b.last_ok_at > timedelta(hours=hours))
        ):
            issues.append(Issue(job, "warning", f"{label} keeps failing", b.last_error[:200]))
    today = now.date()
    if now.hour >= 8 and session.scalar(select(func.max(Digest.day))) != today:
        issues.append(
            Issue("digest", "warning", "No morning digest today", "the 07:00 job did not run")
        )
    if now.hour >= 4 and session.scalar(select(func.max(ModelHistory.day))) not in (
        today,
        today - timedelta(days=1),
    ):
        issues.append(
            Issue(
                "snapshot", "warning", "No model snapshot last night", "the 02:30 job did not run"
            )
        )
    return issues
