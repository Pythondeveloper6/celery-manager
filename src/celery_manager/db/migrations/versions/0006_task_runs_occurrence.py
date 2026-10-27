"""task_runs.occurrence_at / beat_sent_at: explicit occurrence identity

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-27 10:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("task_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("occurrence_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("beat_sent_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_index(
            batch_op.f("ix_task_runs_occurrence_at"), ["occurrence_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("task_runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_task_runs_occurrence_at"))
        batch_op.drop_column("beat_sent_at")
        batch_op.drop_column("occurrence_at")
