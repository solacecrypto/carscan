"""Admin notifications (scraper blocked / parser broken).

Uses the plain Bot API over httpx so it can be called from any thread (the scrape jobs run in
scheduler threads, the bot itself in the asyncio loop)."""

import httpx
import structlog

from app.config import get_settings

log = structlog.get_logger()


def notify_group(text: str) -> None:
    """Plain message to the deals group (no buttons). Used for pipeline milestones."""
    settings = get_settings()
    token = settings.telegram_bot_token.get_secret_value()
    if not token or not settings.telegram_chat_id:
        log.info("group.notify.skipped", text=text)
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": settings.telegram_chat_id, "text": text[:4000], "parse_mode": "HTML"},
            timeout=10,
        ).raise_for_status()
    except httpx.HTTPError as e:
        log.warning("group.notify.failed", error=str(e))


def notify_admin(text: str) -> None:
    log.error("admin.notify", text=text)
    settings = get_settings()
    token = settings.telegram_bot_token.get_secret_value()
    chat = settings.telegram_admin_chat_id or settings.telegram_chat_id
    if not token or not chat:
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": f"⚠️ {text}"[:4000]},
            timeout=10,
        ).raise_for_status()
    except httpx.HTTPError as e:
        log.warning("admin.notify.failed", error=str(e))
