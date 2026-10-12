import secrets
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import User, get_sessionmaker


def get_db() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


DB = Annotated[Session, Depends(get_db)]


class LoginRequired(Exception):
    def __init__(self, next_url: str) -> None:
        self.next_url = next_url


def current_user(request: Request, db: DB) -> User:
    user_id = request.session.get("user_id")
    user = db.get(User, user_id) if user_id else None
    if user is None:
        request.session.clear()
        raise LoginRequired(request.url.path if request.method == "GET" else "/")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def login_required_handler(request: Request, exc: LoginRequired) -> RedirectResponse:
    if request.headers.get("HX-Request"):
        resp = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        resp.headers["HX-Redirect"] = "/login"
        return resp
    return RedirectResponse(f"/login?next={exc.next_url}", status_code=status.HTTP_303_SEE_OTHER)


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def csrf_protect(request: Request) -> None:
    """Dependency for every state-changing route: token must come back in the form or header."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    expected = request.session.get("csrf")
    supplied = request.headers.get("X-CSRF-Token")
    if not supplied and request.headers.get("content-type", "").startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    ):
        form = await request.form()
        supplied = str(form.get("csrf_token", ""))
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token missing or invalid")
