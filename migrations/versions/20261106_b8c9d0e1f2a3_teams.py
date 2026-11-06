"""teams

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-11-06 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    plan = sa.Enum("individual", "team", name="team_plan", native_enum=False, length=16)
    op.create_table(
        "teams",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=80), nullable=False, unique=True),
        sa.Column("plan", plan, nullable=False, server_default="team"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.add_column(
        "users",
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id", ondelete="SET NULL")),
    )
    op.create_index("ix_users_team_id", "users", ["team_id"])
    # Everyone who exists today has been working one shared board: keep it that way by
    # putting them all on one team with the team plan.
    conn = op.get_bind()
    if conn.scalar(sa.text("select count(*) from users")):
        conn.execute(sa.text("insert into teams (name, plan) values ('Carscan', 'team')"))
        conn.execute(
            sa.text("update users set team_id = (select id from teams where name = 'Carscan')")
        )


def downgrade() -> None:
    op.drop_index("ix_users_team_id", table_name="users")
    op.drop_column("users", "team_id")
    op.drop_table("teams")
