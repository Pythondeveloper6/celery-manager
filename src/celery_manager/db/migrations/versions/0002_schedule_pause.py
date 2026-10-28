"""schedule pause: periodic_schedules.paused_at / paused_by

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-26 08:41:13.175870

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def _columns() -> list[sa.Column]:
    return [
        sa.Column('paused_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('paused_by', sa.String(length=255), nullable=True),
    ]


def upgrade() -> None:
    # Pre-release 0.1.0a4 builds added these two columns without Alembic, one
    # ALTER at a time. A database that has both is stamped straight at this
    # revision (celery_manager.db.migrate); one that got only the first runs
    # every revision and lands here, so add only what is missing. New
    # revisions don't need this: every database that reaches them is
    # versioned.
    existing = set()
    if not op.get_context().as_sql:
        existing = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('periodic_schedules', schema=op.get_context().opts.get("version_table_schema"))}
    missing = [column for column in _columns() if column.name not in existing]
    if not missing:
        return
    with op.batch_alter_table('periodic_schedules', schema=op.get_context().opts.get("version_table_schema")) as batch_op:
        for column in missing:
            batch_op.add_column(column)


def downgrade() -> None:
    with op.batch_alter_table('periodic_schedules', schema=op.get_context().opts.get("version_table_schema")) as batch_op:
        batch_op.drop_column('paused_by')
        batch_op.drop_column('paused_at')
