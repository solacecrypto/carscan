"""Telegram group alerts: send pending alerts, handle Claim/Pass/Contacted buttons, and keep
the message in sync with status changes made on the website."""

from __future__ import annotations

import asyncio
import html
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from app.alerts.queue import pending_alerts
from app.db import Alert, AlertStatus, Listing, User, db_session

log = structlog.get_logger()

MAX_SEND_ATTEMPTS = 3
SEND_PAUSE_SECONDS = 3.0  # Telegram allows ~20 group messages per minute


class BotLike(Protocol):
    """The subset of telegram.Bot we use; tests pass a fake."""

    async def send_photo(self, chat_id: str, photo: str, caption: str, **kw: Any) -> Any: ...
    async def send_message(self, chat_id: str, text: str, **kw: Any) -> Any: ...
    async def edit_message_caption(
        self, chat_id: str, message_id: int, caption: str, **kw: Any
    ) -> Any: ...
    async def edit_message_text(
        self, chat_id: str, message_id: int, text: str, **kw: Any
    ) -> Any: ...


# ---- rendering ------------------------------------------------------------------------
def _status_line(alert: Alert) -> str:
    if alert.status is AlertStatus.new:
        return ""
    who = alert.claimed_by.username if alert.claimed_by else "?"
    icon = {"claimed": "🙋", "contacted": "📞", "passed": "⏭"}.get(alert.status.value, "")
    return f"\n\n{icon} <b>{alert.status.value}</b> by {html.escape(who)}"


def format_alert(alert: Alert, site_url: str, old_price: int | None = None) -> str:
    listing: Listing = alert.listing
    e = html.escape
    title = f"{listing.make} {listing.model}"
    if listing.variant:
        title += f" · {listing.variant}"
    head = "🔻 <b>Price drop</b>" if alert.kind == "price_drop" else "🆕 <b>New listing</b>"
    price = f"€ {listing.price_eur:,}".replace(",", ".")
    if alert.kind == "price_drop" and (old_price or listing.previous_price_eur):
        old = old_price or listing.previous_price_eur
        price = f"<s>€ {old:,}</s> → <b>{price}</b>".replace(",", ".")
    else:
        price = f"<b>{price}</b>"
    year = listing.first_registration.strftime("%m/%Y") if listing.first_registration else "–"
    km = f"{listing.mileage_km:,} km".replace(",", ".") if listing.mileage_km is not None else "–"
    place = " ".join(x for x in (listing.postcode, listing.city) if x) or "–"
    seller = listing.seller_type.value + (
        f" · {e(listing.seller_name)}" if listing.seller_name else ""
    )
    lines = [
        f"{head} · {e(alert.saved_search.name)}",
        f"<b>{e(title)}</b>",
        price,
        f"{year} · {km} · {listing.gearbox or '–'}"
        + (f" · {listing.power_kw} kW" if listing.power_kw else ""),
        f"📍 {e(place)} ({listing.country}) · {seller}",
        f'<a href="{e(listing.url)}">Original ad</a> · '
        f'<a href="{site_url}/listings/{listing.id}">On our site</a>',
    ]
    return "\n".join(lines) + _status_line(alert)


def build_keyboard(alert: Alert) -> InlineKeyboardMarkup | None:
    buttons = [
        InlineKeyboardButton(label, callback_data=f"alert:{alert.id}:{value}")
        for value, label in (
            ("claimed", "🙋 Claim"),
            ("contacted", "📞 Contacted"),
            ("passed", "⏭ Pass"),
        )
        if alert.status.value != value
    ]
    if alert.status is not AlertStatus.new:
        buttons.append(InlineKeyboardButton("↩ reset", callback_data=f"alert:{alert.id}:new"))
    return InlineKeyboardMarkup([buttons])


_LOAD = (
    selectinload(Alert.listing),
    selectinload(Alert.saved_search),
    selectinload(Alert.claimed_by),
)


# ---- sending --------------------------------------------------------------------------
async def deliver_pending(
    bot: BotLike, chat_id: str, site_url: str, pause: float = SEND_PAUSE_SECONDS
) -> int:
    """Send every unsent alert. Returns the number sent."""
    with db_session() as s:
        ids = [a.id for a in pending_alerts(s) if a.send_attempts < MAX_SEND_ATTEMPTS]
    sent = 0
    for alert_id in ids:
        with db_session() as s:
            alert = s.scalar(select(Alert).where(Alert.id == alert_id).options(*_LOAD))
            if alert is None or alert.sent_at is not None:
                continue
            listing = alert.listing
            caption = format_alert(alert, site_url)
            kb = build_keyboard(alert)
            try:
                if listing.photos:
                    msg = await bot.send_photo(
                        chat_id=chat_id,
                        photo=listing.photos[0],
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                else:
                    msg = await bot.send_message(
                        chat_id=chat_id,
                        text=caption,
                        parse_mode="HTML",
                        reply_markup=kb,
                        disable_web_page_preview=True,
                    )
            except TelegramError as e:
                alert.send_attempts += 1
                alert.send_error = str(e)[:500]
                log.warning(
                    "telegram.send.failed",
                    alert=alert.id,
                    attempt=alert.send_attempts,
                    error=str(e),
                )
                continue
            alert.sent_at = datetime.now(UTC)
            alert.telegram_message_id = msg.message_id
            alert.send_error = None
            alert.message_synced = True
            sent += 1
        await asyncio.sleep(pause)
    return sent


async def sync_unsynced(bot: BotLike, chat_id: str, site_url: str) -> int:
    """Edit Telegram messages whose alert status changed on the website."""
    with db_session() as s:
        ids = list(
            s.scalars(
                select(Alert.id).where(
                    Alert.message_synced.is_(False), Alert.telegram_message_id.isnot(None)
                )
            )
        )
    done = 0
    for alert_id in ids:
        with db_session() as s:
            alert = s.scalar(select(Alert).where(Alert.id == alert_id).options(*_LOAD))
            if alert is None:
                continue
            try:
                await _edit(bot, chat_id, alert, site_url)
            except TelegramError as e:
                log.warning("telegram.sync.failed", alert=alert.id, error=str(e))
            alert.message_synced = True  # don't retry forever on a deleted message
            done += 1
    return done


async def _edit(bot: BotLike, chat_id: str, alert: Alert, site_url: str) -> None:
    text = format_alert(alert, site_url)
    kb = build_keyboard(alert)
    assert alert.telegram_message_id is not None
    if alert.listing.photos:
        await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=alert.telegram_message_id,
            caption=text,
            parse_mode="HTML",
            reply_markup=kb,
        )
    else:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=alert.telegram_message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )


# ---- button presses -------------------------------------------------------------------
def apply_button(data: str, telegram_username: str | None) -> tuple[int | None, str]:
    """Apply a callback payload 'alert:<id>:<status>'. Returns (alert_id, toast text)."""
    try:
        _, raw_id, status_value = data.split(":")
        alert_id, new_status = int(raw_id), AlertStatus(status_value)
    except ValueError:
        return None, "Unknown button"
    with db_session() as s:
        alert = s.scalar(select(Alert).where(Alert.id == alert_id).options(*_LOAD))
        if alert is None:
            return None, "Alert no longer exists"
        user = (
            s.scalar(select(User).where(User.telegram_username.ilike(telegram_username)))
            if telegram_username
            else None
        )
        if user is None:
            return alert_id, "Your Telegram username is not linked to an account. Ask an admin."
        if new_status is not AlertStatus.new and alert.status is new_status:
            who = alert.claimed_by.username if alert.claimed_by else "?"
            return alert_id, f"Already {new_status.value} by {who}"
        alert.status = new_status
        alert.claimed_by = user if new_status is not AlertStatus.new else None
        alert.status_changed_at = datetime.now(UTC)
        alert.message_synced = False
        return alert_id, f"{new_status.value} ✓" if new_status is not AlertStatus.new else "reset ✓"


async def on_callback(update: Any, context: Any) -> None:
    """python-telegram-bot CallbackQueryHandler entry point."""
    query = update.callback_query
    if query is None or not query.data:
        return
    username = query.from_user.username if query.from_user else None
    alert_id, toast = apply_button(query.data, username)
    await query.answer(toast, show_alert=alert_id is None or "not linked" in toast)
    if alert_id is None:
        return
    settings = _settings()
    with db_session() as s:
        alert = s.scalar(select(Alert).where(Alert.id == alert_id).options(*_LOAD))
        if alert is None or alert.telegram_message_id is None:
            return
        try:
            await _edit(context.bot, settings.telegram_chat_id, alert, settings.site_url)
        except TelegramError as e:
            log.warning("telegram.edit.failed", alert=alert_id, error=str(e))
        alert.message_synced = True


def _settings():  # type: ignore[no-untyped-def]  # tiny indirection so tests can patch
    from app.config import get_settings

    return get_settings()
