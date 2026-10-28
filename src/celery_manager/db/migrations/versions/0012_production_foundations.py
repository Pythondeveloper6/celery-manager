"""Bounded output, initiator identity and external schedule definitions."""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("task_runs", schema=op.get_context().opts.get("version_table_schema")) as batch:
        batch.add_column(sa.Column("log_output_truncated", sa.Boolean(), server_default="0", nullable=False))
        batch.add_column(sa.Column("result_repr", sa.Text(), nullable=True))
        batch.add_column(sa.Column("result_truncated", sa.Boolean(), server_default="0", nullable=False))
        batch.add_column(sa.Column("initiator", sa.String(255), nullable=True))
        batch.create_index("ix_task_runs_initiator", ["initiator"])
    with op.batch_alter_table("periodic_schedules", schema=op.get_context().opts.get("version_table_schema")) as batch:
        batch.add_column(sa.Column("definition", sa.Text(), nullable=True))
        batch.add_column(sa.Column("source", sa.String(64), nullable=True))
    with op.batch_alter_table("audit_log", schema=op.get_context().opts.get("version_table_schema")) as batch:
        batch.add_column(sa.Column("actor_subject", sa.String(255), nullable=True))


def downgrade():
    with op.batch_alter_table("audit_log", schema=op.get_context().opts.get("version_table_schema")) as batch:
        batch.drop_column("actor_subject")
    with op.batch_alter_table("periodic_schedules", schema=op.get_context().opts.get("version_table_schema")) as batch:
        batch.drop_column("source")
        batch.drop_column("definition")
    with op.batch_alter_table("task_runs", schema=op.get_context().opts.get("version_table_schema")) as batch:
        batch.drop_index("ix_task_runs_initiator")
        for name in ("initiator", "result_truncated", "result_repr", "log_output_truncated"):
            batch.drop_column(name)
