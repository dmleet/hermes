"""album genres

Revision ID: 12c394282762
Revises: 3838ad3a7bf6
Create Date: 2026-09-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "12c394282762"
down_revision = "3838ad3a7bf6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # MusicBrainz release-group genres for the pages (services/genres.py). Nullable and left
    # NULL for existing targets, which is what the genres job looks for (active rows first).
    with op.batch_alter_table("album_target") as batch_op:
        batch_op.add_column(sa.Column("genres", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("album_target") as batch_op:
        batch_op.drop_column("genres")
