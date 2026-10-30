"""Execution origin, tenant and bounded observation context."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import VARCHAR

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().opts.get("version_table_schema")
    for column in [sa.Column("source", sa.String(32), nullable=False, server_default="celery"),
                   sa.Column("tenant_id", sa.Unicode(255).with_variant(VARCHAR(255, charset="utf8mb4"), "mysql"), nullable=True),
                   sa.Column("context_json", sa.Text(), nullable=True),
                   sa.Column("operation_key", sa.Unicode(255).with_variant(VARCHAR(255, charset="utf8mb4"), "mysql"), nullable=True),
                   sa.Column("correlation_id", sa.Unicode(255).with_variant(VARCHAR(255, charset="utf8mb4"), "mysql"), nullable=True)]:
        op.add_column("task_runs", column, schema=schema)
    for name in ("source", "tenant_id", "operation_key", "correlation_id"):
        op.create_index("ix_task_runs_" + name, "task_runs", [name], schema=schema)


def downgrade():
    schema = op.get_context().opts.get("version_table_schema")
    for name in ("correlation_id", "operation_key", "context_json", "tenant_id", "source"):
        if name != "context_json":
            op.drop_index("ix_task_runs_" + name, table_name="task_runs", schema=schema)
        op.drop_column("task_runs", name, schema=schema, mssql_drop_default=name == "source")
