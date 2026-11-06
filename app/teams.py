"""Who sees whose deals.

A user without a team, or on a team with the *individual* plan, works alone: only their own
deals are visible and only they can own them. On a team with the *team* plan every member sees
the whole board and can hand deals to each other.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Team, User


def teammates(session: Session, user: User) -> list[User]:
    """The users whose deals `user` may see and own, always including `user`."""
    team = session.get(Team, user.team_id) if user.team_id else None
    if team is None or not team.shared:
        return [user]
    return list(
        session.scalars(select(User).where(User.team_id == team.id).order_by(User.username))
    )


def visible_user_ids(session: Session, user: User) -> set[int]:
    return {u.id for u in teammates(session, user)}


def team_of(session: Session, user: User) -> Team | None:
    """Loaded through the session: the current user object may be detached by the time a
    page renders, so no lazy `user.team` here."""
    return session.get(Team, user.team_id) if user.team_id else None


def plan_label(session: Session, user: User) -> str:
    """For the account page: 'team · Carscan' or 'individual'."""
    team = team_of(session, user)
    if team is None:
        return "individual"
    return f"{team.plan.value} · {team.name}"
