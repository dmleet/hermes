"""candidate info_url

Revision ID: 79bd96acdd0d
Revises: c1d2e3f4a5b6
Create Date: 2026-09-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "79bd96acdd0d"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable: rows searched before this migration have no page to link to, and not every
    # indexer reports one.
    with op.batch_alter_table("candidate") as batch_op:
        batch_op.add_column(sa.Column("info_url", sa.String(length=1024), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("candidate") as batch_op:
        batch_op.drop_column("info_url")
