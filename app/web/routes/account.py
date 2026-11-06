"""Own account (password) and, for admins, the user list."""

from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.config import get_settings
from app.db import Team, TeamPlan, User
from app.geo import geocode_address
from app.teams import plan_label, team_of, teammates
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
        {
            "title": "Account",
            "msg": request.query_params.get("msg"),
            "err": None,
            "plan": plan_label(db, user),
            "telegram_on": get_settings().telegram_enabled,
            "team": team_of(db, user),
            "teammates": [u for u in teammates(db, user) if u.id != user.id],
        },
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
    teams = list(db.scalars(select(Team).order_by(Team.name)))
    return templates.TemplateResponse(
        request,
        "account/users.html",
        {
            "title": "Users",
            "users": users,
            "teams": teams,
            "plans": list(TeamPlan),
            "msg": request.query_params.get("msg"),
        },
    )


@router.post("/teams", dependencies=[Depends(csrf_protect)])
def teams_create(
    db: DB, user: CurrentUser, name: str = Form(""), plan: str = Form("team")
) -> RedirectResponse:
    _admin(user)
    name = name.strip()[:80]
    if not name:
        raise HTTPException(400, "a team needs a name")
    if db.scalar(select(Team).where(Team.name == name)):
        raise HTTPException(409, "that team exists")
    db.add(Team(name=name, plan=_plan(plan), created_at=datetime.now(UTC)))
    db.flush()
    return RedirectResponse(
        f"/users?msg=Created+team+{name}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/teams/{team_id}/{action}", dependencies=[Depends(csrf_protect)])
def teams_action(
    db: DB, user: CurrentUser, team_id: int, action: str, plan: str = Form("")
) -> RedirectResponse:
    _admin(user)
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(404)
    if action == "plan":
        team.plan = _plan(plan)
        msg = f"{team.name}+is+on+the+{team.plan.value}+plan"
    elif action == "delete":
        for member in team.members:  # members stay, they just work alone from now on
            member.team_id = None
        db.delete(team)
        msg = f"Deleted+team+{team.name}"
    else:
        raise HTTPException(400)
    db.flush()
    return RedirectResponse(f"/users?msg={msg}", status_code=status.HTTP_303_SEE_OTHER)


def _plan(value: str) -> TeamPlan:
    try:
        return TeamPlan(value)
    except ValueError:
        raise HTTPException(400, "plan must be individual or team") from None


@router.post("/users", dependencies=[Depends(csrf_protect)])
def users_create(
    db: DB,
    user: CurrentUser,
    username: str = Form(""),
    password: str = Form(""),
    telegram_username: str = Form(""),
    is_admin: str = Form(""),
    team_id: str = Form(""),
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
            team_id=int(team_id) if team_id.isdigit() else None,
            created_at=datetime.now(UTC),
        )
    )
    db.flush()
    return RedirectResponse(f"/users?msg=Created+{username}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/users/{user_id}/{action}", dependencies=[Depends(csrf_protect)])
def users_action(
    db: DB,
    user: CurrentUser,
    user_id: int,
    action: str,
    password: str = Form(""),
    team_id: str = Form(""),
) -> RedirectResponse:
    _admin(user)
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404)
    if action == "team":
        team = db.get(Team, int(team_id)) if team_id.isdigit() else None
        if team_id and team is None:
            raise HTTPException(404, "no such team")
        target.team = team
        joined = "joined+" + team.name.replace(" ", "+") if team else "works+alone+now"
        msg = f"{target.username}+{joined}"
    elif action == "reset":
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


def _loc_redirect(next_path: str, flag: str) -> RedirectResponse:
    """Back to where the globe popover was opened, with a one-word result for it."""
    path = next_path if next_path.startswith("/") and "//" not in next_path else "/account"
    return RedirectResponse(f"{path}?loc={flag}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/account/location", dependencies=[Depends(csrf_protect)])
def set_location(
    request: Request,
    db: DB,
    user: CurrentUser,
    address: str = Form(""),
    next: str = Form("/account"),
) -> HTMLResponse:
    """Save where this person drives from; empty clears it (the configured base applies)."""
    address = " ".join(address.split())[:200]
    if not address:
        user.home_label = user.home_lat = user.home_lon = None
        db.flush()
        return _loc_redirect(next, "cleared")
    try:
        with httpx.Client() as client:
            hit = geocode_address(client, address)
    except httpx.HTTPError:
        return _loc_redirect(next, "down")
    if hit is None:
        return _loc_redirect(next, "notfound")
    user.home_lat, user.home_lon, user.home_label = hit[0], hit[1], hit[2][:200]
    db.flush()
    return _loc_redirect(next, "saved")
