"""listing colour

Revision ID: e5b7c9d1f3a2
Revises: d1a4b6c8e2f0
Create Date: 2026-10-16 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5b7c9d1f3a2"
down_revision: str | None = "d1a4b6c8e2f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("listings", sa.Column("color", sa.String(length=16), nullable=True))
    op.create_index("ix_listings_color", "listings", ["color"])
    op.create_index("ix_listings_body_type", "listings", ["body_type"])


def downgrade() -> None:
    op.drop_index("ix_listings_body_type", table_name="listings")
    op.drop_index("ix_listings_color", table_name="listings")
    op.drop_column("listings", "color")
