"""user home location

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-10-26 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("home_label", sa.String(length=200), nullable=True))
    op.add_column("users", sa.Column("home_lat", sa.Float(), nullable=True))
    op.add_column("users", sa.Column("home_lon", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "home_lon")
    op.drop_column("users", "home_lat")
    op.drop_column("users", "home_label")
