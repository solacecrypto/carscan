"""repair estimates

Revision ID: e2f7b9a4c1d6
Revises: c4e9a7b21f3d
Create Date: 2026-10-15 21:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e2f7b9a4c1d6"
down_revision: str | None = "c4e9a7b21f3d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("listings", sa.Column("repair_cost_eur", sa.Integer(), nullable=True))
    op.add_column(
        "listings",
        sa.Column("repair_breakdown", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("listings", "repair_breakdown")
    op.drop_column("listings", "repair_cost_eur")
