import gzip
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import backup
from app.db import User


def test_dump_round_trip(db: Session, tmp_path: Path) -> None:
    try:
        backup._bin("pg_dump")
    except FileNotFoundError:
        pytest.skip("no pg_dump available")
    db.add(User(username="dumped", password_hash="x"))
    db.commit()
    url = str(db.get_bind().url.render_as_string(hide_password=False))
    path = backup.create(tmp_path, url=url)
    assert path.exists() and path.name.startswith("carscan-") and path.suffix == ".gz"
    with gzip.open(path, "rb") as f:
        assert f.read(5) == b"PGDMP"  # custom format header
    assert backup.listing(tmp_path) == [(path.name, path.stat().st_size)]
    # wipe the table, restore, and the row is back
    db.execute(text("DELETE FROM users"))
    db.commit()
    backup.restore(path, url=url)
    db.expire_all()
    assert db.query(User).filter_by(username="dumped").count() == 1
    # pruning keeps the newest files
    for i in range(3):
        (tmp_path / f"carscan-2026010{i}-0000.dump.gz").write_bytes(b"old")
    assert backup.prune(tmp_path, keep=2) == 2
    assert len(list(tmp_path.glob("carscan-*.dump.gz"))) == 2
