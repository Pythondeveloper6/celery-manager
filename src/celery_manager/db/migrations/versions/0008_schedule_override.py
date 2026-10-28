"""periodic_schedules: operator schedule edits

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-28 09:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # All nullable with no backfill: NULL means "run as the code says",
    # which is exactly what every existing schedule does today.
    with op.batch_alter_table("periodic_schedules", schema=None) as batch_op:
        batch_op.add_column(sa.Column("schedule_override", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("edited_by", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    # Drops every saved edit: after a downgrade, schedules run as the code
    # says again (an older version has nowhere to read an edit from).
    with op.batch_alter_table("periodic_schedules", schema=None) as batch_op:
        batch_op.drop_column("edited_at")
        batch_op.drop_column("edited_by")
        batch_op.drop_column("schedule_override")
