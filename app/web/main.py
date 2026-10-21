import structlog
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db import User, get_sessionmaker
from app.logging import configure_logging, configure_sentry
from app.web.deps import LoginRequired, login_required_handler
from app.web.routes import account, auth, deals, extras, listings, models, searches, status
from app.web.templating import STATIC_DIR, TEMPLATES_DIR


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    configure_sentry("web")
    problems = settings.check_production_secrets()
    if problems:
        raise RuntimeError("refusing to start with placeholder secrets: " + "; ".join(problems))
    app = FastAPI(title="Car Scanner", docs_url=None, redoc_url=None, openapi_url=None)
    secure = settings.domain not in ("localhost", "127.0.0.1")
    app.add_exception_handler(LoginRequired, login_required_handler)  # type: ignore[arg-type]

    @app.middleware("http")
    async def attach_user(request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        request.state.user = None
        user_id = request.session.get("user_id")
        if user_id:
            with get_sessionmaker()() as s:
                request.state.user = s.get(User, user_id)
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response

    # added last = outermost, so the session is available to attach_user above
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key.get_secret_value(),
        session_cookie="carscan_session",
        max_age=30 * 24 * 3600,
        same_site="lax",
        https_only=secure,
    )

    if settings.role == "all":
        # one container: the scheduler runs inside the web process (Telegram polling needs
        # its own loop and stays with ROLE=worker)
        from app.worker.main import build_scheduler

        scheduler = build_scheduler(settings.scrape_interval_minutes)

        @app.on_event("startup")
        def _start_scheduler() -> None:
            scheduler.start()
            if settings.telegram_enabled:
                structlog.get_logger().warning(
                    "telegram.not_polling", hint="ROLE=all does not poll Telegram; use ROLE=worker"
                )

        @app.on_event("shutdown")
        def _stop_scheduler() -> None:
            scheduler.shutdown(wait=False)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(auth.router)
    app.include_router(listings.router)
    app.include_router(deals.router)
    app.include_router(searches.router)
    app.include_router(status.router)
    app.include_router(models.router)
    app.include_router(extras.router)
    app.include_router(account.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    assert TEMPLATES_DIR.exists()
    return app


app = create_app()
