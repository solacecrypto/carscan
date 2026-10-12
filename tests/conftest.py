import os
import tempfile
from collections.abc import Iterator

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret")

_pg = None
if "DATABASE_URL" not in os.environ:
    import pgserver

    _pg = pgserver.get_server(tempfile.mkdtemp(prefix="carscan-pg-"))
    os.environ["DATABASE_URL"] = _pg.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import Base, get_engine, get_sessionmaker  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _migrated_db() -> Iterator[None]:
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    yield
    get_engine().dispose()
    if _pg is not None:
        _pg.cleanup()


@pytest.fixture
def db(_migrated_db: None) -> Iterator[Session]:
    """A session on a clean database (all tables truncated before the test)."""
    tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()
