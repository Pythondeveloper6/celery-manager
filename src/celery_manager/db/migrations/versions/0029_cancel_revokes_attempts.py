"""Cooperative cancel columns, durable revokes and per-attempt history."""
import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def _label():
    return sa.String(255).with_variant(sa.Unicode(255), "mssql")


def _text():
    return sa.Text().with_variant(sa.UnicodeText(), "mssql")


def upgrade():
    schema = op.get_context().opts.get("version_table_schema")
    with op.batch_alter_table("task_runs", schema=schema) as batch_op:
        batch_op.add_column(sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("cancel_requested_by", _label(), nullable=True))
    op.create_table(
        "revoked_tasks",
        sa.Column("task_id", sa.String(255), primary_key=True),
        sa.Column("terminate", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("requested_by", _label(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        schema=schema,
    )
    op.create_index("ix_revoked_tasks_expires_at", "revoked_tasks", ["expires_at"], schema=schema)
    op.create_table(
        "task_run_attempts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(255), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("worker", _label(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("runtime_seconds", sa.Float(), nullable=True),
        sa.Column("exception", _text(), nullable=True),
        sa.Column("traceback", _text(), nullable=True),
        sa.UniqueConstraint("task_id", "attempt", name="uq_task_run_attempts_task_id_attempt"),
        schema=schema,
    )
    op.create_index("ix_task_run_attempts_task_id", "task_run_attempts", ["task_id"], schema=schema)
    op.create_index("ix_task_run_attempts_finished_at", "task_run_attempts", ["finished_at"], schema=schema)


def downgrade():
    schema = op.get_context().opts.get("version_table_schema")
    op.drop_table("task_run_attempts", schema=schema)
    op.drop_table("revoked_tasks", schema=schema)
    with op.batch_alter_table("task_runs", schema=schema) as batch_op:
        batch_op.drop_column("cancel_requested_by")
        batch_op.drop_column("cancel_requested_at")
