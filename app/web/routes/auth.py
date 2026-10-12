from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.db import User
from app.web.deps import DB, csrf_protect, csrf_token
from app.web.ratelimit import login_limiter
from app.web.security import verify_password
from app.web.templating import templates

router = APIRouter()


def _safe_next(value: str | None) -> str:
    return value if value and value.startswith("/") and not value.startswith("//") else "/"


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str | None = None) -> HTMLResponse:
    if request.session.get("user_id"):
        return RedirectResponse(_safe_next(next), status_code=status.HTTP_303_SEE_OTHER)
    csrf_token(request)
    return templates.TemplateResponse(
        request, "login.html", {"next": _safe_next(next), "error": None}
    )


@router.post("/login", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
def login(
    request: Request,
    db: DB,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
) -> HTMLResponse:
    ip = request.client.host if request.client else "?"
    keys = (f"ip:{ip}", f"user:{username.lower()}")
    ctx = {"next": _safe_next(next), "error": None}
    if any(login_limiter.is_limited(k) for k in keys):
        ctx["error"] = "Too many attempts. Try again in 15 minutes."
        return templates.TemplateResponse(request, "login.html", ctx, status_code=429)
    user = db.scalar(select(User).where(User.username == username.strip()))
    if user is None or not verify_password(password, user.password_hash):
        for k in keys:
            login_limiter.hit(k)
        ctx["error"] = "Wrong username or password."
        return templates.TemplateResponse(request, "login.html", ctx, status_code=401)
    for k in keys:
        login_limiter.reset(k)
    request.session.clear()
    request.session["user_id"] = user.id
    csrf_token(request)
    return RedirectResponse(_safe_next(next), status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout", dependencies=[Depends(csrf_protect)])
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
