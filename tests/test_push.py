from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app import push
from app.db import PushSubscription, User


def test_keygen_and_sending_with_dead_devices(db: Session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    pub, priv = push.keygen()
    assert len(pub) > 80 and len(priv) > 40
    monkeypatch.setattr("app.config.get_settings", lambda: _settings(pub, priv))
    monkeypatch.setattr(push, "get_settings", lambda: _settings(pub, priv))
    u = User(username="p", password_hash="x")
    db.add(u)
    db.flush()
    good = PushSubscription(
        user_id=u.id,
        endpoint="https://push/ok",
        keys={"p256dh": "a", "auth": "b"},
        created_at=datetime.now(UTC),
    )
    dead = PushSubscription(
        user_id=u.id,
        endpoint="https://push/dead",
        keys={"p256dh": "a", "auth": "b"},
        created_at=datetime.now(UTC),
    )
    db.add_all([good, dead])
    db.flush()
    calls: list[dict] = []  # type: ignore[type-arg]

    class Gone(Exception):
        def __init__(self) -> None:
            self.response = type("R", (), {"status_code": 410})()

    def fake(**kw):  # type: ignore[no-untyped-def]
        calls.append(kw)
        if "dead" in kw["subscription_info"]["endpoint"]:
            raise Gone()

    monkeypatch.setattr(push, "_send", fake)
    assert push.send_to_user(db, u.id, "t", "b", "/x", "price_down") == 1
    assert len(calls) == 2 and '"url": "/x"' in calls[0]["data"]
    assert db.query(PushSubscription).count() == 1  # the dead one was removed


def _settings(pub: str, priv: str):  # type: ignore[no-untyped-def]
    from app.config import Settings

    return Settings(
        database_url="postgresql://x/y",
        secret_key="s",
        vapid_public_key=pub,
        vapid_private_key=priv,
    )
