"""task_runs.updated_at: the change feed behind live dashboard updates

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-26 08:59:49.960424

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Three steps rather than one NOT NULL add: SQLite refuses `ADD COLUMN`
    # with a non-constant default, and existing rows deserve a better value
    # than "the moment of the upgrade" anyway. So add it nullable, backfill
    # from the latest timestamp each row already carries, then tighten it
    # (batch mode recreates the table for that on SQLite; elsewhere it is a
    # plain ALTER).
    with op.batch_alter_table('task_runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True))

    op.execute(
        "UPDATE task_runs SET updated_at = "
        "COALESCE(finished_at, progress_updated_at, started_at, CURRENT_TIMESTAMP)"
    )

    with op.batch_alter_table('task_runs', schema=None) as batch_op:
        batch_op.alter_column(
            'updated_at',
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        )
        batch_op.create_index(batch_op.f('ix_task_runs_updated_at'), ['updated_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('task_runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_task_runs_updated_at'))
        batch_op.drop_column('updated_at')
