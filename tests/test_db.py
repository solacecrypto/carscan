from datetime import UTC, datetime

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from app.cli import app as cli
from app.db import Base, Listing, SellerType, User, get_engine
from app.web.security import hash_password, verify_password


def test_migrations_match_models(db: Session) -> None:
    with get_engine().connect() as conn:
        ctx = MigrationContext.configure(conn)
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == [], diff


def test_password_hashing() -> None:
    h = hash_password("correct horse")
    assert h.startswith("$argon2")
    assert verify_password("correct horse", h)
    assert not verify_password("wrong", h)


def test_cli_create_and_list_users(db: Session) -> None:
    r = CliRunner().invoke(
        cli,
        ["users", "create", "quinten", "--telegram-username", "@q"],
        input="secret123\nsecret123\n",
    )
    assert r.exit_code == 0, r.output
    r = CliRunner().invoke(cli, ["users", "list"])
    assert "quinten" in r.output and "telegram=q" in r.output
    r = CliRunner().invoke(cli, ["users", "create", "quinten"], input="secret123\nsecret123\n")
    assert r.exit_code != 0 and "already exists" in r.output


def test_listing_roundtrip(db: Session) -> None:
    now = datetime.now(UTC)
    db.add(
        Listing(
            source="autoscout24_de",
            source_listing_id="abc",
            url="https://example.com/abc",
            make="BMW",
            model="320i",
            price_eur=5500,
            seller_type=SellerType.private,
            country="DE",
            photos=["https://example.com/1.jpg"],
            raw={"k": "v"},
            first_seen=now,
            last_seen=now,
        )
    )
    db.commit()
    row = db.scalar(select(Listing).where(Listing.source_listing_id == "abc"))
    assert row is not None and row.seller_type is SellerType.private and row.is_active
    assert db.scalar(select(User)) is None
