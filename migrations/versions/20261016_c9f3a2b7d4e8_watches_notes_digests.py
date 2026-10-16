"""watches, listing notes, digests

Revision ID: c9f3a2b7d4e8
Revises: b8e2d4c6a1f9
Create Date: 2026-10-16 13:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c9f3a2b7d4e8"
down_revision: str | None = "b8e2d4c6a1f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "watches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "listing_id",
            sa.Integer(),
            sa.ForeignKey("listings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("target_price_eur", sa.Integer(), nullable=True),
        sa.Column("notify_gone", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("listing_id", "user_id", name="uq_watches_listing_user"),
    )
    op.create_index("ix_watches_listing_id", "watches", ["listing_id"])
    op.create_table(
        "listing_notes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "listing_id",
            sa.Integer(),
            sa.ForeignKey("listings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_listing_notes_listing_id", "listing_notes", ["listing_id"])
    op.create_table(
        "digests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("day", sa.Date(), nullable=False, unique=True),
        sa.Column("items", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("digests")
    op.drop_index("ix_listing_notes_listing_id", table_name="listing_notes")
    op.drop_table("listing_notes")
    op.drop_index("ix_watches_listing_id", table_name="watches")
    op.drop_table("watches")
