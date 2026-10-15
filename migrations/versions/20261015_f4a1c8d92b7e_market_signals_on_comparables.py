"""market signals on comparables

Revision ID: f4a1c8d92b7e
Revises: e2f7b9a4c1d6
Create Date: 2026-10-15 22:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4a1c8d92b7e"
down_revision: str | None = "e2f7b9a4c1d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("comparables", sa.Column("listed_at", sa.Date(), nullable=True))
    op.add_column("comparables", sa.Column("demand", sa.String(length=16), nullable=True))
    op.add_column("comparables", sa.Column("reserved", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("comparables", "reserved")
    op.drop_column("comparables", "demand")
    op.drop_column("comparables", "listed_at")
