"""photo flags and geo cache

Revision ID: d1a4b6c8e2f0
Revises: c9f3a2b7d4e8
Create Date: 2026-10-16 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d1a4b6c8e2f0"
down_revision: str | None = "c9f3a2b7d4e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "listing_insights",
        sa.Column("photo_flags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_table(
        "geo_cache",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=200), nullable=False, unique=True),
        sa.Column("country", sa.String(length=2), nullable=False),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("geo_cache")
    op.drop_column("listing_insights", "photo_flags")
