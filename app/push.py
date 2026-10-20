"""Web push: the bell, but in your pocket. Browsers subscribe on the Account page; every
in-app notification is also sent to the user's subscribed devices. Needs VAPID keys
(``carscan push keygen``) and HTTPS."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import PushSubscription

log = structlog.get_logger()
MAX_FAILURES = 5

# swapped out in tests
_send: Callable[..., Any] | None = None


def enabled() -> bool:
    st = get_settings()
    return bool(st.vapid_public_key and st.vapid_private_key.get_secret_value())


def keygen() -> tuple[str, str]:
    """Fresh VAPID key pair as URL-safe base64 strings (public, private)."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    priv = key.private_numbers().private_value.to_bytes(32, "big")
    pub = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")  # noqa: E731
    return b64(pub), b64(priv)


def send_to_user(
    session: Session, user_id: int, title: str, body: str | None, url: str | None, tag: str
) -> int:
    """Push to every device of one user; subscriptions that are gone are removed."""
    if not enabled():
        return 0
    subs = list(
        session.scalars(select(PushSubscription).where(PushSubscription.user_id == user_id))
    )
    if not subs:
        return 0
    st = get_settings()
    payload = json.dumps(
        {"title": title, "body": body or "", "url": url or "/notifications", "tag": tag}
    )
    sent = 0
    for sub in subs:
        try:
            sender = _send or _default_send
            sender(
                subscription_info={"endpoint": sub.endpoint, "keys": sub.keys},
                data=payload,
                vapid_private_key=st.vapid_private_key.get_secret_value(),
                vapid_claims={"sub": f"mailto:{st.vapid_contact or 'admin@example.com'}"},
                ttl=3600,
            )
            sub.failures = 0
            sent += 1
        except Exception as e:  # noqa: BLE001 - one dead device must not stop the others
            status = getattr(getattr(e, "response", None), "status_code", None)
            sub.failures += 1
            log.warning(
                "push.failed", endpoint=sub.endpoint[:60], status=status, error=str(e)[:120]
            )
            if status in (404, 410) or sub.failures >= MAX_FAILURES:
                session.delete(sub)
    session.flush()
    return sent


def _default_send(**kwargs: Any) -> Any:
    from pywebpush import webpush

    return webpush(**kwargs)
