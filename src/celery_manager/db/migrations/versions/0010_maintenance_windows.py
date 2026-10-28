"""maintenance_windows table; task_runs.skip_reason

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-28 11:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "maintenance_windows",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("days_of_week", sa.String(length=32), nullable=False),
        sa.Column("start_time", sa.String(length=5), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("applies_to", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    schema=op.get_context().opts.get("version_table_schema"))
    with op.batch_alter_table("maintenance_windows", schema=op.get_context().opts.get("version_table_schema")) as batch_op:
        batch_op.create_index(batch_op.f("ix_maintenance_windows_name"), ["name"], unique=True)

    # Nullable, no backfill: skips recorded before this have no reason.
    with op.batch_alter_table("task_runs", schema=op.get_context().opts.get("version_table_schema")) as batch_op:
        batch_op.add_column(sa.Column("skip_reason", sa.String(length=255), nullable=True))


def downgrade() -> None:
    # Drops every window (an older version never skips on one, so the
    # schedules simply run) and the reasons on past skipped runs.
    with op.batch_alter_table("task_runs", schema=op.get_context().opts.get("version_table_schema")) as batch_op:
        batch_op.drop_column("skip_reason")
    with op.batch_alter_table("maintenance_windows", schema=op.get_context().opts.get("version_table_schema")) as batch_op:
        batch_op.drop_index(batch_op.f("ix_maintenance_windows_name"))
    op.drop_table("maintenance_windows", schema=op.get_context().opts.get("version_table_schema"))
