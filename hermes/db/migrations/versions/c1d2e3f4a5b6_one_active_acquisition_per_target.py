"""one active acquisition per target

Revision ID: c1d2e3f4a5b6
Revises: 234ba9a2550b
Create Date: 2026-09-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c1d2e3f4a5b6"
down_revision = "234ba9a2550b"
branch_labels = None
depends_on = None

_ACTIVE = "state NOT IN ('ALREADY_OWNED', 'REJECTED', 'IMPORTED', 'CANCELLED')"


def upgrade() -> None:
    op.create_index(
        "uq_acquisition_active_target",
        "acquisition",
        ["album_target_id"],
        unique=True,
        sqlite_where=sa.text(_ACTIVE),
    )


def downgrade() -> None:
    op.drop_index("uq_acquisition_active_target", table_name="acquisition")
