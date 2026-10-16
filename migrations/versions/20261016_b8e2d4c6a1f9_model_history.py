"""model history snapshots

Revision ID: b8e2d4c6a1f9
Revises: a7d3e5f1b2c4
Create Date: 2026-10-16 12:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8e2d4c6a1f9"
down_revision: str | None = "a7d3e5f1b2c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model_key", sa.String(length=64), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("n_active", sa.Integer(), nullable=False),
        sa.Column("n_valued", sa.Integer(), nullable=False),
        sa.Column("n_positive", sa.Integer(), nullable=False),
        sa.Column("median_price_eur", sa.Integer(), nullable=True),
        sa.Column("median_landed_eur", sa.Integer(), nullable=True),
        sa.Column("median_nl_value_eur", sa.Integer(), nullable=True),
        sa.Column("median_spread_eur", sa.Integer(), nullable=True),
        sa.Column("n_comparables", sa.Integer(), nullable=False),
        sa.UniqueConstraint("model_key", "day", name="uq_model_history_model_day"),
    )
    op.create_index("ix_model_history_model_key", "model_history", ["model_key"])


def downgrade() -> None:
    op.drop_index("ix_model_history_model_key", table_name="model_history")
    op.drop_table("model_history")
