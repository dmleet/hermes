"""album art

Revision ID: 3838ad3a7bf6
Revises: 79bd96acdd0d
Create Date: 2026-09-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "3838ad3a7bf6"
down_revision = "79bd96acdd0d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Album art for the pages (services/art.py). Server defaults backfill every existing
    # target as pending, so the art job picks them up (active rows first).
    with op.batch_alter_table("album_target") as batch_op:
        batch_op.add_column(
            sa.Column("art_status", sa.String(length=16), server_default="pending", nullable=False)
        )
        batch_op.add_column(sa.Column("art_checked_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("art_failures", sa.Integer(), server_default="0", nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("album_target") as batch_op:
        batch_op.drop_column("art_failures")
        batch_op.drop_column("art_checked_at")
        batch_op.drop_column("art_status")
