from sqlalchemy import func, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from app.cli import COMPARABLE_SEARCHES, E90_PARAMS, PHASE4_SEARCHES
from app.cli import app as cli
from app.db import SavedSearch, User
from app.watchlist import build_specs, load_models, sync


def test_watchlist_matches_hand_seeded_searches() -> None:
    specs = {
        (s.source, s.name.split(" · ")[-1] if s.name.count(" · ") == 2 else ""): s
        for s in build_specs(load_models()[0])
    }
    by_source = {}
    for s in specs.values():
        by_source.setdefault(s.source, []).append(s)
    assert set(by_source) == {
        "autoscout24_de",
        "autoscout24_be",
        "autoscout24_it",
        "tweedehands_be",
        "kleinanzeigen",
        "automobile_it",
        "bytbil",
        "autoscout24_nl",
        "marktplaats",
    }
    assert by_source["autoscout24_de"][0].params == E90_PARAMS
    hand = {src: p for _, src, p, _ in PHASE4_SEARCHES}
    assert by_source["autoscout24_be"][0].params == hand["autoscout24_be"]
    assert by_source["autoscout24_it"][0].params == hand["autoscout24_it"]
    assert by_source["automobile_it"][0].params == hand["automobile_it"]
    assert by_source["bytbil"][0].params == hand["bytbil"]
    assert {s.params["query"] for s in by_source["kleinanzeigen"]} == {"318i", "320i"}
    assert {s.params["query"] for s in by_source["tweedehands_be"]} == {"318i", "320i"}
    nl = {src: p for _, src, p in COMPARABLE_SEARCHES}
    assert by_source["autoscout24_nl"][0].params == nl["autoscout24_nl"]
    assert (
        by_source["autoscout24_nl"][0].role == "comparables"
        and by_source["marktplaats"][0].role == "comparables"
    )
    assert all(s.role == "deals" for s in by_source["kleinanzeigen"])


def test_sync_is_idempotent_and_prunes(db: Session) -> None:
    u = User(username="q", password_hash="x")
    db.add(u)
    db.commit()
    models = load_models()
    r = sync(db, u, models)
    db.commit()
    assert r["created"] == 12 and r["updated"] == 0
    assert sync(db, u, models) == {"created": 0, "updated": 0, "paused": 0}
    extra = dict(
        models[0],
        key="other",
        label="Other car",
        autoscout24={"path": "/lst/vw/golf", "cat": "ma74gr1"},
        sources={"deals": ["autoscout24_de"], "comparables": []},
    )
    assert sync(db, u, models + [extra])["created"] == 1
    r = sync(db, u, models, prune=True)
    db.commit()
    assert r["paused"] == 1
    assert (
        db.scalar(
            select(SavedSearch).where(SavedSearch.name == "Other car · autoscout24_de")
        ).active
        is False
    )  # type: ignore[union-attr]
    out = CliRunner().invoke(cli, ["models", "sync", "--owner", "q", "--dry-run"]).output
    assert "autoscout24_de" in out and "BMW E90/E91" in out
    assert "12 searches" in CliRunner().invoke(cli, ["models", "list"]).output


def test_sync_adopts_hand_seeded_searches(db: Session) -> None:
    db.add(User(username="q", password_hash="x"))
    db.commit()
    r = CliRunner().invoke(cli, ["searches", "seed", "--owner", "q"])
    assert r.exit_code == 0
    CliRunner().invoke(cli, ["searches", "seed-comparables", "--owner", "q"])
    CliRunner().invoke(cli, ["searches", "seed-phase4", "--owner", "q"])
    assert db.scalar(select(func.count()).select_from(SavedSearch)) == 13
    u = db.scalar(select(User))
    r2 = sync(db, u, load_models(), prune=True)  # type: ignore[arg-type]
    db.commit()
    assert r2["created"] == 0 and r2["updated"] == 12 and r2["paused"] == 1
    assert db.scalar(select(func.count()).select_from(SavedSearch)) == 13
    gp = db.scalar(select(SavedSearch).where(SavedSearch.source == "gaspedaal"))
    assert gp is not None and not gp.active
