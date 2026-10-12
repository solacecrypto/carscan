from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from telegram.error import TelegramError

from app.alerts.telegram import (
    apply_button,
    build_keyboard,
    deliver_pending,
    format_alert,
    on_callback,
    sync_unsynced,
)
from app.db import Alert, AlertStatus, Listing, SavedSearch, SellerType, User

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


class FakeBot:
    def __init__(self, fail_first: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._fail = fail_first
        self._next_id = 100

    async def _record(self, name: str, kw: dict) -> SimpleNamespace:
        if self._fail:
            self._fail = False
            raise TelegramError("boom")
        self.calls.append((name, kw))
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    async def send_photo(self, **kw):  # type: ignore[no-untyped-def]
        return await self._record("send_photo", kw)

    async def send_message(self, **kw):  # type: ignore[no-untyped-def]
        return await self._record("send_message", kw)

    async def edit_message_caption(self, **kw):  # type: ignore[no-untyped-def]
        return await self._record("edit_message_caption", kw)

    async def edit_message_text(self, **kw):  # type: ignore[no-untyped-def]
        return await self._record("edit_message_text", kw)


@pytest.fixture
def alerts(db: Session) -> dict:
    u = User(username="quinten", password_hash="x", telegram_username="QuintenTG")
    s = SavedSearch(name="E90 search", owner=u, source="autoscout24_de")
    a = Listing(
        source="autoscout24_de",
        source_listing_id="a",
        url="https://as24/a",
        make="BMW",
        model="320i",
        variant="320i Touring <Edition>",
        price_eur=6000,
        previous_price_eur=7000,
        mileage_km=150000,
        first_registration=datetime(2010, 5, 1).date(),
        gearbox="manual",
        power_kw=125,
        seller_type=SellerType.dealer,
        seller_name="Dealer & Co",
        country="DE",
        city="Berlin",
        postcode="10115",
        photos=["https://img/1.webp"],
        landed_cost_eur=7700,
        bpm_eur=1046,
        nl_value_eur=9300,
        spread_eur=1600,
        spread_pct=20.8,
        first_seen=NOW,
        last_seen=NOW,
        saved_searches=[s],
    )
    b = Listing(
        source="autoscout24_de",
        source_listing_id="b",
        url="https://as24/b",
        make="BMW",
        model="318i",
        price_eur=4000,
        seller_type=SellerType.private,
        country="DE",
        first_seen=NOW,
        last_seen=NOW,
        saved_searches=[s],
    )
    al_a = Alert(listing=a, saved_search=s, kind="price_drop", created_at=NOW)
    al_b = Alert(listing=b, saved_search=s, kind="new", created_at=NOW)
    db.add_all([u, s, a, b, al_a, al_b])
    db.commit()
    return {"a": al_a.id, "b": al_b.id, "user": u.id}


def test_format_alert_escapes_and_shows_drop(db: Session, alerts: dict) -> None:
    alert = db.get(Alert, alerts["a"])
    assert alert is not None
    text = format_alert(alert, "https://cars.example.com")
    assert "🔻 <b>Price drop</b> · E90 search" in text
    assert "&lt;Edition&gt;" in text and "Dealer &amp; Co" in text
    assert "<s>€ 7.000</s> → <b>€ 6.000</b>" in text
    assert "05/2010 · 150.000 km · manual · 125 kW" in text
    assert "📍 10115 Berlin (DE) · dealer" in text
    assert "🇳🇱 landed ≈ <b>€ 7.700</b> (BPM € 1.046)" in text
    assert "📈 NL value ≈ € 9.300 · spread <b>+€ 1.600</b> (20.8%)" in text
    assert (
        'href="https://as24/a"' in text
        and f'href="https://cars.example.com/listings/{alert.listing_id}"' in text
    )
    assert "by" not in text.split("On our site")[1]
    kb = build_keyboard(alert).inline_keyboard[0]
    assert [b.callback_data for b in kb] == [
        f"alert:{alert.id}:claimed",
        f"alert:{alert.id}:contacted",
        f"alert:{alert.id}:passed",
    ]


async def test_deliver_pending_sends_and_retries(db: Session, alerts: dict) -> None:
    bot = FakeBot(fail_first=True)
    sent = await deliver_pending(bot, "-100123", "https://site", pause=0)
    assert sent == 1  # first alert failed once, second sent
    db.expire_all()
    a, b = db.get(Alert, alerts["a"]), db.get(Alert, alerts["b"])
    assert a is not None and b is not None
    assert a.sent_at is None and a.send_attempts == 1 and a.send_error == "boom"
    assert b.sent_at is not None and b.telegram_message_id == 101
    assert bot.calls[0][0] == "send_message"  # listing b has no photo

    sent = await deliver_pending(bot, "-100123", "https://site", pause=0)
    assert sent == 1
    db.expire_all()
    db.refresh(a)
    assert a.sent_at is not None and a.telegram_message_id == 102 and a.send_error is None
    name, kw = bot.calls[1]
    assert (
        name == "send_photo" and kw["photo"] == "https://img/1.webp" and kw["parse_mode"] == "HTML"
    )
    assert "Price drop" in kw["caption"] and kw["reply_markup"] is not None
    assert await deliver_pending(bot, "-100123", "https://site", pause=0) == 0


async def test_button_press_updates_status_and_edits_message(
    db: Session, alerts: dict, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    bot = FakeBot()
    await deliver_pending(bot, "-100123", "https://site", pause=0)
    bot.calls.clear()
    monkeypatch.setattr(
        "app.alerts.telegram._settings",
        lambda: SimpleNamespace(telegram_chat_id="-100123", site_url="https://site"),
    )
    answers: list[tuple] = []

    async def answer(text: str, show_alert: bool = False) -> None:
        answers.append((text, show_alert))

    def update(data: str, username: str | None) -> SimpleNamespace:
        return SimpleNamespace(
            callback_query=SimpleNamespace(
                data=data, from_user=SimpleNamespace(username=username), answer=answer
            )
        )

    ctx = SimpleNamespace(bot=bot)
    aid = alerts["a"]
    await on_callback(update(f"alert:{aid}:claimed", "quintentg"), ctx)  # case-insensitive match
    assert answers[-1] == ("claimed ✓", False)
    db.expire_all()
    a = db.get(Alert, aid)
    assert (
        a is not None
        and a.status is AlertStatus.claimed
        and a.claimed_by.username == "quinten"
        and a.message_synced
    )
    name, kw = bot.calls[-1]
    assert name == "edit_message_caption" and "🙋 <b>claimed</b> by quinten" in kw["caption"]
    assert any(b.callback_data == f"alert:{aid}:new" for b in kw["reply_markup"].inline_keyboard[0])

    await on_callback(update(f"alert:{aid}:claimed", "quintentg"), ctx)
    assert answers[-1][0].startswith("Already claimed by quinten")

    await on_callback(update(f"alert:{aid}:passed", "stranger"), ctx)
    assert "not linked" in answers[-1][0] and answers[-1][1] is True
    db.expire_all()
    db.refresh(a)
    assert a.status is AlertStatus.claimed

    await on_callback(update("alert:999999:passed", "quintentg"), ctx)
    assert answers[-1] == ("Alert no longer exists", True)
    assert apply_button("garbage", "quintentg") == (None, "Unknown button")


async def test_website_change_is_synced_to_telegram(db: Session, alerts: dict) -> None:
    bot = FakeBot()
    await deliver_pending(bot, "-100123", "https://site", pause=0)
    bot.calls.clear()
    b = db.get(Alert, alerts["b"])
    assert b is not None
    b.status = AlertStatus.contacted
    b.claimed_by = db.get(User, alerts["user"])
    b.message_synced = False
    db.commit()
    assert await sync_unsynced(bot, "-100123", "https://site") == 1
    name, kw = bot.calls[-1]
    assert name == "edit_message_text" and "📞 <b>contacted</b> by quinten" in kw["text"]
    db.expire_all()
    assert db.scalar(select(Alert.message_synced).where(Alert.id == alerts["b"])) is True
    assert await sync_unsynced(bot, "-100123", "https://site") == 0
