"""task_runs.sent_at / received_at; queue_depth_samples table

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-28 14:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable, no backfill: runs recorded before this have no send time, and
    # a wait can't be derived from anything else.
    with op.batch_alter_table("task_runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("received_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "queue_depth_samples",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("queue", sa.String(length=255), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("queue_depth_samples", schema=None) as batch_op:
        batch_op.create_index("ix_queue_depth_samples_queue_sampled_at", ["queue", "sampled_at"], unique=False)


def downgrade() -> None:
    # Drops the recorded send/receive times and every depth sample.
    with op.batch_alter_table("queue_depth_samples", schema=None) as batch_op:
        batch_op.drop_index("ix_queue_depth_samples_queue_sampled_at")
    op.drop_table("queue_depth_samples")
    with op.batch_alter_table("task_runs", schema=None) as batch_op:
        batch_op.drop_column("received_at")
        batch_op.drop_column("sent_at")
