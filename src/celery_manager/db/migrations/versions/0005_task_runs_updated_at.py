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
    with op.batch_alter_table('task_runs', schema=op.get_context().opts.get("version_table_schema")) as batch_op:
        batch_op.add_column(sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True))

    from celery_manager.db.dialect import utcnow
    table = sa.table("task_runs", sa.column("updated_at"), sa.column("finished_at"),
                     sa.column("progress_updated_at"), sa.column("started_at"),
                     schema=op.get_context().opts.get("version_table_schema"))
    op.execute(table.update().values(updated_at=sa.func.coalesce(
        table.c.finished_at, table.c.progress_updated_at, table.c.started_at, utcnow())))

    with op.batch_alter_table('task_runs', schema=op.get_context().opts.get("version_table_schema")) as batch_op:
        batch_op.alter_column(
            'updated_at',
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
            server_default=utcnow(),
        )
        batch_op.create_index(batch_op.f('ix_task_runs_updated_at'), ['updated_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('task_runs', schema=op.get_context().opts.get("version_table_schema")) as batch_op:
        batch_op.drop_index(batch_op.f('ix_task_runs_updated_at'))
        batch_op.drop_column('updated_at')
