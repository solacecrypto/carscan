from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from app.alerts import queue_alerts
from app.cli import app as cli
from app.db import Listing, SavedSearch, SellerType, User
from app.storage import UpsertResult, is_same_car, link_duplicates, relink_all

T0 = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)


def _l(
    source: str, sid: str, price: int, km: int, reg: tuple[int, int] = (2011, 8), **kw: object
) -> Listing:
    base = dict(
        source=source,
        source_listing_id=sid,
        url=f"https://{source}/{sid}",
        make="BMW",
        model="320i",
        body_type="estate",
        price_eur=price,
        mileage_km=km,
        first_registration=datetime(reg[0], reg[1], 1).date(),
        seller_type=SellerType.dealer,
        country="DE",
        first_seen=T0,
        last_seen=T0,
        raw={},
    )
    return Listing(**{**base, **kw})


def test_is_same_car_rules() -> None:
    a = _l("autoscout24_de", "a", 8000, 150_000)
    assert is_same_car(a, _l("kleinanzeigen", "b", 8100, 152_000))
    assert not is_same_car(a, _l("autoscout24_de", "b", 8000, 150_000))  # same source
    assert not is_same_car(a, _l("kleinanzeigen", "b", 9000, 150_000))  # price off by >3%
    assert not is_same_car(a, _l("kleinanzeigen", "b", 8000, 160_000))  # mileage off by >2%
    assert not is_same_car(
        a, _l("kleinanzeigen", "b", 8000, 150_000, reg=(2011, 9))
    )  # month differs
    assert is_same_car(
        a, _l("kleinanzeigen", "b", 8000, 150_000, reg=(2011, 1), raw={"year_only": True})
    )
    assert not is_same_car(a, _l("kleinanzeigen", "b", 8000, 150_000, body_type="sedan"))
    assert is_same_car(a, _l("kleinanzeigen", "b", 8000, 150_000, body_type=None))
    assert not is_same_car(a, _l("tweedehands_be", "b", 8000, 150_000, country="BE"))
    assert not is_same_car(a, _l("kleinanzeigen", "b", 8000, None))  # type: ignore[arg-type]


def test_link_and_alert_suppression(db: Session) -> None:
    u = User(username="u", password_hash="x")
    s1 = SavedSearch(name="as24", owner=u, source="autoscout24_de")
    s2 = SavedSearch(name="ka", owner=u, source="kleinanzeigen")
    a = _l("autoscout24_de", "a", 8000, 150_000, saved_searches=[s1])
    b = _l(
        "kleinanzeigen",
        "b",
        8100,
        151_000,
        first_seen=T0 + timedelta(hours=1),
        last_seen=T0 + timedelta(hours=1),
        saved_searches=[s2],
    )
    c = _l("kleinanzeigen", "c", 5000, 151_000, saved_searches=[s2])
    db.add_all([u, s1, s2, a, b, c])
    db.commit()
    r = link_duplicates(db, [b, c], T0)
    db.commit()
    assert (
        r.linked == 1
        and b.duplicate_of_id == a.id
        and a.duplicate_of_id is None
        and c.duplicate_of_id is None
    )
    alerts = queue_alerts(db, s2, UpsertResult(new=[b, c]), T0)
    assert [x.listing_id for x in alerts] == [c.id]
    # relink keeps the earliest-seen listing canonical
    assert relink_all(db, T0).linked == 1
    db.commit()
    db.expire_all()
    assert db.get(Listing, b.id).duplicate_of_id == a.id  # type: ignore[union-attr]
    out = CliRunner().invoke(cli, ["dedup", "run"]).output
    assert "linked 1" in out
    assert db.scalar(select(Listing).where(Listing.id == a.id)).duplicates[0].id == b.id  # type: ignore[union-attr]


def test_cli_seed_phase4(db: Session) -> None:
    db.add(User(username="q", password_hash="x"))
    db.commit()
    r = CliRunner().invoke(cli, ["searches", "seed-phase4", "--owner", "q"])
    assert r.exit_code == 0 and "created 8" in r.output, r.output
    out = CliRunner().invoke(cli, ["searches", "list"]).output
    for src in (
        "autoscout24_be",
        "tweedehands_be",
        "autoscout24_it",
        "automobile_it",
        "kleinanzeigen",
        "bytbil",
    ):
        assert src in out
    s = db.scalar(select(SavedSearch).where(SavedSearch.source == "bytbil"))
    assert s is not None and s.source_params["motor_regex"] == r"\b(318\s?i|320\s?i)\b"
