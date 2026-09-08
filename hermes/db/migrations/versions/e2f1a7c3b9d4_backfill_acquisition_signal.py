"""backfill acquisition.signal_id from event data

Revision ID: e2f1a7c3b9d4
Revises: d18dc61b90a7
Create Date: 2026-09-07
"""

from __future__ import annotations

from alembic import op

revision = "e2f1a7c3b9d4"
down_revision = "d18dc61b90a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Acquisitions created before the signal link existed recorded the signal id on their
    # first transition event. Copy it over so unresolved requests can be named.
    op.execute(
        """
        UPDATE acquisition
        SET signal_id = (
            SELECT CAST(json_extract(event.data, '$.signal_id') AS INTEGER)
            FROM event
            WHERE event.acquisition_id = acquisition.id
              AND json_extract(event.data, '$.signal_id') IS NOT NULL
            ORDER BY event.id
            LIMIT 1
        )
        WHERE signal_id IS NULL
        """
    )


def downgrade() -> None:
    pass  # data only; the column itself is dropped by d18dc61b90a7's downgrade
