"""tuv_until on listing insights

Revision ID: a7d3e5f1b2c4
Revises: f4a1c8d92b7e
Create Date: 2026-10-16 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7d3e5f1b2c4"
down_revision: str | None = "f4a1c8d92b7e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("listing_insights", sa.Column("tuv_until", sa.Date(), nullable=True))
    op.create_index("ix_listing_insights_tuv_until", "listing_insights", ["tuv_until"])


def downgrade() -> None:
    op.drop_index("ix_listing_insights_tuv_until", table_name="listing_insights")
    op.drop_column("listing_insights", "tuv_until")
