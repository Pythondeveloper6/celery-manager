"""task_runs.status_source: where a run's status came from when it was not a live event."""
import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().opts.get("version_table_schema")
    with op.batch_alter_table("task_runs", schema=schema) as batch_op:
        batch_op.add_column(sa.Column("status_source", sa.String(16), nullable=True))


def downgrade():
    schema = op.get_context().opts.get("version_table_schema")
    with op.batch_alter_table("task_runs", schema=schema) as batch_op:
        batch_op.drop_column("status_source")
