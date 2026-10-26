"""Own account (password) and, for admins, the user list."""

from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.db import User
from app.geo import geocode_address
from app.web.deps import DB, CurrentUser, csrf_protect
from app.web.security import hash_password, verify_password
from app.web.templating import templates

router = APIRouter()
MIN_PASSWORD = 8


def _admin(user: User) -> User:
    if not user.is_admin:
        raise HTTPException(403, "admins only")
    return user


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, db: DB, user: CurrentUser) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "account/index.html",
        {"title": "Account", "msg": request.query_params.get("msg"), "err": None},
    )


@router.post("/account/password", dependencies=[Depends(csrf_protect)])
def change_password(
    request: Request,
    db: DB,
    user: CurrentUser,
    current: str = Form(""),
    new: str = Form(""),
    repeat: str = Form(""),
) -> HTMLResponse:
    err = None
    if not verify_password(current, user.password_hash):
        err = "The current password is wrong."
    elif len(new) < MIN_PASSWORD:
        err = f"The new password needs at least {MIN_PASSWORD} characters."
    elif new != repeat:
        err = "The two new passwords differ."
    if err:
        return templates.TemplateResponse(
            request,
            "account/index.html",
            {"title": "Account", "msg": None, "err": err},
            status_code=400,
        )
    user.password_hash = hash_password(new)
    db.flush()
    return RedirectResponse("/account?msg=Password+changed", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, db: DB, user: CurrentUser) -> HTMLResponse:
    _admin(user)
    users = list(db.scalars(select(User).order_by(User.id)))
    return templates.TemplateResponse(
        request,
        "account/users.html",
        {"title": "Users", "users": users, "msg": request.query_params.get("msg")},
    )


@router.post("/users", dependencies=[Depends(csrf_protect)])
def users_create(
    db: DB,
    user: CurrentUser,
    username: str = Form(""),
    password: str = Form(""),
    telegram_username: str = Form(""),
    is_admin: str = Form(""),
) -> RedirectResponse:
    _admin(user)
    username = username.strip()
    if not username or len(password) < MIN_PASSWORD:
        raise HTTPException(400, f"username and a password of {MIN_PASSWORD}+ characters")
    if db.scalar(select(User).where(User.username == username)):
        raise HTTPException(409, "that username exists")
    db.add(
        User(
            username=username,
            password_hash=hash_password(password),
            telegram_username=telegram_username.strip().lstrip("@") or None,
            is_admin=is_admin == "true",
            created_at=datetime.now(UTC),
        )
    )
    db.flush()
    return RedirectResponse(f"/users?msg=Created+{username}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/users/{user_id}/{action}", dependencies=[Depends(csrf_protect)])
def users_action(
    db: DB, user: CurrentUser, user_id: int, action: str, password: str = Form("")
) -> RedirectResponse:
    _admin(user)
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404)
    if action == "reset":
        if len(password) < MIN_PASSWORD:
            raise HTTPException(400, f"a password of {MIN_PASSWORD}+ characters")
        target.password_hash = hash_password(password)
        msg = f"Password+reset+for+{target.username}"
    elif action == "toggle_admin":
        if target.id == user.id:
            raise HTTPException(400, "you cannot change your own admin flag")
        target.is_admin = not target.is_admin
        msg = f"{target.username}+is+{'now' if target.is_admin else 'no+longer'}+admin"
    elif action == "delete":
        if target.id == user.id:
            raise HTTPException(400, "you cannot delete yourself")
        db.delete(target)
        msg = f"Deleted+{target.username}"
    else:
        raise HTTPException(400)
    db.flush()
    return RedirectResponse(f"/users?msg={msg}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/account/location", dependencies=[Depends(csrf_protect)])
def set_location(
    request: Request, db: DB, user: CurrentUser, address: str = Form("")
) -> HTMLResponse:
    """Save where this person drives from; empty clears it (the configured base applies)."""
    address = " ".join(address.split())[:200]
    if not address:
        user.home_label = user.home_lat = user.home_lon = None
        db.flush()
        return RedirectResponse(
            "/account?msg=Location+cleared,+distances+use+the+default+base",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    try:
        with httpx.Client() as client:
            hit = geocode_address(client, address)
    except httpx.HTTPError:
        hit = None
        err = "The map service did not answer; try again in a minute."
    else:
        err = None if hit else "That place was not found. Try a postcode, or add the country."
    if err or hit is None:
        return templates.TemplateResponse(
            request,
            "account/index.html",
            {"title": "Account", "msg": None, "err": err, "address": address},
            status_code=400,
        )
    user.home_lat, user.home_lon, user.home_label = hit[0], hit[1], hit[2][:200]
    db.flush()
    return RedirectResponse("/account?msg=Location+saved", status_code=status.HTTP_303_SEE_OTHER)
