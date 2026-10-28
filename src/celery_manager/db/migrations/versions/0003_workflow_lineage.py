"""workflow lineage: task_runs.parent_id / root_id / group_id

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-26 10:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

_COLUMNS = ('parent_id', 'root_id', 'group_id')


def upgrade() -> None:
    with op.batch_alter_table('task_runs', schema=op.get_context().opts.get("version_table_schema")) as batch_op:
        for name in _COLUMNS:
            batch_op.add_column(sa.Column(name, sa.String(length=255), nullable=True))
            batch_op.create_index(batch_op.f(f'ix_task_runs_{name}'), [name], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('task_runs', schema=op.get_context().opts.get("version_table_schema")) as batch_op:
        for name in reversed(_COLUMNS):
            batch_op.drop_index(batch_op.f(f'ix_task_runs_{name}'))
            batch_op.drop_column(name)
