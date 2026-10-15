"""Background worker.

- APScheduler (threads): runs every active saved search every N minutes.
- python-telegram-bot (asyncio, main thread): delivers alerts, handles button presses, and
  keeps messages in sync. When Telegram is not configured the main thread just idles.
"""

import signal
import time
from datetime import UTC, datetime

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import Settings, get_settings
from app.logging import configure_logging
from app.worker.jobs import analyze_backlog, enrich_backlog, run_due_searches

log = structlog.get_logger()


def build_scheduler(interval_minutes: int) -> BackgroundScheduler:
    """Every few minutes, run the searches whose own interval has elapsed."""
    scheduler = BackgroundScheduler(timezone="UTC")
    # One job only: max_instances=1 keeps a long pass from overlapping with the next tick.
    # (A separate start-up job would run concurrently with it and double-run searches.)
    scheduler.add_job(
        run_due_searches,
        IntervalTrigger(minutes=min(5, interval_minutes), jitter=30),
        id="scrape_due",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
        next_run_time=datetime.now(UTC),
    )
    scheduler.add_job(
        analyze_backlog,
        IntervalTrigger(minutes=10, jitter=60),
        id="insights_backlog",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        enrich_backlog,
        IntervalTrigger(minutes=15, jitter=60),
        id="detail_backlog",
        max_instances=1,
        coalesce=True,
    )
    return scheduler


def run_telegram(settings: Settings) -> None:
    from telegram.ext import Application, CallbackQueryHandler, ContextTypes

    from app.alerts.telegram import deliver_pending, on_callback, sync_unsynced

    chat_id, site_url = settings.telegram_chat_id, settings.site_url

    async def tick(context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            n = await deliver_pending(context.bot, chat_id, site_url)
            m = await sync_unsynced(context.bot, chat_id, site_url)
            if n or m:
                log.info("telegram.tick", sent=n, synced=m)
        except Exception as e:  # noqa: BLE001 - keep the loop alive
            log.error("telegram.tick.failed", error=str(e))

    application = (
        Application.builder().token(settings.telegram_bot_token.get_secret_value()).build()
    )
    application.add_handler(CallbackQueryHandler(on_callback, pattern=r"^alert:\d+:\w+$"))
    assert application.job_queue is not None
    application.job_queue.run_repeating(tick, interval=20, first=5)
    log.info("telegram.polling")
    application.run_polling(allowed_updates=["callback_query"], close_loop=False)


def idle_forever() -> None:
    stop = False

    def _stop(*_: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while not stop:
        time.sleep(1)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    scheduler = build_scheduler(settings.scrape_interval_minutes)
    scheduler.start()
    log.info(
        "worker.started",
        interval_minutes=settings.scrape_interval_minutes,
        telegram=settings.telegram_enabled,
    )
    try:
        if settings.telegram_enabled:
            run_telegram(settings)
        else:
            log.warning("telegram.disabled", hint="set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
            idle_forever()
    finally:
        scheduler.shutdown()
        log.info("worker.stopped")


if __name__ == "__main__":
    main()
