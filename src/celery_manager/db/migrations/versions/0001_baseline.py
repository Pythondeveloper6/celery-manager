"""baseline: the schema of 0.1.0a1-0.1.0a3

Identical in all three releases, which created it with `create_all` and no
version table. Running this revision on such a database creates only what's
missing and then versions it (celery_manager.db.migrate).

Revision ID: 0001
Revises:
Create Date: 2026-10-26 08:41:06.607545

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Creates only the tables that don't exist yet. A database from
    # 0.1.0a1-0.1.0a3 normally has all of them and is simply versioned by
    # this revision; one missing some (say, created by hand from a subset of
    # the models) gets them here. New revisions don't need this: every
    # database that reaches them is versioned.
    existing = set()
    if not op.get_context().as_sql:
        existing = set(sa.inspect(op.get_bind()).get_table_names())

    if 'app_settings' not in existing:
        op.create_table('app_settings',
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('value', sa.Text(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_by', sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint('key')
        )

    if 'audit_log' not in existing:
        op.create_table('audit_log',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('username', sa.String(length=255), nullable=False),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('target_type', sa.String(length=64), nullable=False),
        sa.Column('target_id', sa.String(length=255), nullable=False),
        sa.Column('details', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id')
        )

    if 'periodic_schedules' not in existing:
        op.create_table('periodic_schedules',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('task_name', sa.String(length=255), nullable=False),
        sa.Column('schedule_spec', sa.String(length=255), nullable=False),
        sa.Column('args', sa.Text(), nullable=False),
        sa.Column('kwargs', sa.Text(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id')
        )
        with op.batch_alter_table('periodic_schedules', schema=None) as batch_op:
            batch_op.create_index(batch_op.f('ix_periodic_schedules_name'), ['name'], unique=True)

    if 'task_runs' not in existing:
        op.create_table('task_runs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('task_id', sa.String(length=255), nullable=False),
        sa.Column('task_name', sa.String(length=255), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('queue', sa.String(length=255), nullable=True),
        sa.Column('worker', sa.String(length=255), nullable=True),
        sa.Column('args', sa.Text(), nullable=False),
        sa.Column('kwargs', sa.Text(), nullable=False),
        sa.Column('eta', sa.DateTime(timezone=True), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('runtime_seconds', sa.Float(), nullable=True),
        sa.Column('exception', sa.Text(), nullable=True),
        sa.Column('traceback', sa.Text(), nullable=True),
        sa.Column('log_output', sa.Text(), nullable=True),
        sa.Column('retry_count', sa.Integer(), nullable=False),
        sa.Column('schedule_name', sa.String(length=255), nullable=True),
        sa.Column('retried_from_id', sa.String(length=255), nullable=True),
        sa.Column('progress_current', sa.Integer(), nullable=True),
        sa.Column('progress_total', sa.Integer(), nullable=True),
        sa.Column('progress_message', sa.Text(), nullable=True),
        sa.Column('progress_updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id')
        )
        with op.batch_alter_table('task_runs', schema=None) as batch_op:
            batch_op.create_index(batch_op.f('ix_task_runs_retried_from_id'), ['retried_from_id'], unique=False)
            batch_op.create_index(batch_op.f('ix_task_runs_schedule_name'), ['schedule_name'], unique=False)
            batch_op.create_index(batch_op.f('ix_task_runs_status'), ['status'], unique=False)
            batch_op.create_index(batch_op.f('ix_task_runs_task_id'), ['task_id'], unique=True)
            batch_op.create_index(batch_op.f('ix_task_runs_task_name'), ['task_name'], unique=False)

    if 'users' not in existing:
        op.create_table('users',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('username', sa.String(length=255), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint('id')
        )
        with op.batch_alter_table('users', schema=None) as batch_op:
            batch_op.create_index(batch_op.f('ix_users_username'), ['username'], unique=True)

    if 'schedule_overrides' not in existing:
        op.create_table('schedule_overrides',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('schedule_name', sa.String(length=255), nullable=False),
        sa.Column('target_time', sa.DateTime(timezone=True), nullable=False),
        sa.Column('action', sa.String(length=32), nullable=False),
        sa.Column('reschedule_to', sa.DateTime(timezone=True), nullable=True),
        sa.Column('consumed', sa.Boolean(), nullable=False),
        sa.Column('created_by', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['schedule_name'], ['periodic_schedules.name'], ),
        sa.PrimaryKeyConstraint('id')
        )
        with op.batch_alter_table('schedule_overrides', schema=None) as batch_op:
            batch_op.create_index(batch_op.f('ix_schedule_overrides_schedule_name'), ['schedule_name'], unique=False)
            batch_op.create_index(batch_op.f('ix_schedule_overrides_target_time'), ['target_time'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('schedule_overrides', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_schedule_overrides_target_time'))
        batch_op.drop_index(batch_op.f('ix_schedule_overrides_schedule_name'))

    op.drop_table('schedule_overrides')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_username'))

    op.drop_table('users')
    with op.batch_alter_table('task_runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_task_runs_task_name'))
        batch_op.drop_index(batch_op.f('ix_task_runs_task_id'))
        batch_op.drop_index(batch_op.f('ix_task_runs_status'))
        batch_op.drop_index(batch_op.f('ix_task_runs_schedule_name'))
        batch_op.drop_index(batch_op.f('ix_task_runs_retried_from_id'))

    op.drop_table('task_runs')
    with op.batch_alter_table('periodic_schedules', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_periodic_schedules_name'))

    op.drop_table('periodic_schedules')
    op.drop_table('audit_log')
    op.drop_table('app_settings')
