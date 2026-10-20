from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.db import Heartbeat, SavedSearch, ScrapeRun, ScrapeStatus, User
from app.health import beat, check, with_heartbeat

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


def _run(
    db: Session, search: SavedSearch, minutes_ago: int, ok: bool, error: str | None = None
) -> None:
    started = NOW - timedelta(minutes=minutes_ago)
    db.add(
        ScrapeRun(
            source=search.source,
            saved_search_id=search.id,
            started_at=started,
            finished_at=started + timedelta(minutes=1),
            status=ScrapeStatus.ok if ok else ScrapeStatus.failed,
            error=error,
        )  # fmt: skip
    )


def test_rules_fire_on_a_dead_loop_and_a_blocked_source(db: Session) -> None:
    u = User(username="h", password_hash="x")
    ka = SavedSearch(name="ka", owner=u, source="kleinanzeigen")
    de = SavedSearch(name="de", owner=u, source="autoscout24_de")
    db.add_all([u, ka, de])
    db.flush()
    _run(db, de, 5, True)
    for m in (3, 8, 13):
        _run(db, ka, m, False, "BlockedError: HTTP 403")
    db.add(
        Heartbeat(
            job="scrape_due",
            last_run_at=NOW - timedelta(minutes=2),
            last_ok_at=NOW - timedelta(minutes=2),
        )
    )
    db.commit()
    keys = {i.key: i for i in check(db, NOW)}
    assert "source:kleinanzeigen" in keys and "blocked" in keys["source:kleinanzeigen"].title
    assert "scrape_loop" not in keys and "no_runs" not in keys
    # the loop goes quiet
    hb = db.get(Heartbeat, "scrape_due")
    hb.last_run_at = NOW - timedelta(minutes=40)
    db.flush()
    keys = {i.key for i in check(db, NOW)}
    assert "scrape_loop" in keys


def test_heartbeat_wrapper_records_success_and_failure(db: Session) -> None:
    @with_heartbeat("demo")
    def good() -> dict[str, int]:
        return {"done": 3}

    @with_heartbeat("demo_bad")
    def bad() -> None:
        raise RuntimeError("boom")

    assert good() == {"done": 3}
    with pytest.raises(RuntimeError):
        bad()
    beat("plain", True)
    db.expire_all()
    rows = {h.job: h for h in db.query(Heartbeat).all()}
    assert rows["demo"].last_ok_at is not None and rows["demo"].detail == {"done": 3}
    assert rows["demo_bad"].last_ok_at is None and "boom" in (rows["demo_bad"].last_error or "")
    assert rows["plain"].last_ok_at is not None
