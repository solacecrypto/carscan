"""english descriptions

Revision ID: c4e9a7b21f3d
Revises: d97ee8150f70
Create Date: 2026-10-15 19:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e9a7b21f3d"
down_revision: str | None = "d97ee8150f70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("listings", sa.Column("description_en", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("listings", "description_en")
