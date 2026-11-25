"""task_runs.args_redacted: set when stored arguments were redacted."""
import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().opts.get("version_table_schema")
    with op.batch_alter_table("task_runs", schema=schema) as batch_op:
        batch_op.add_column(sa.Column("args_redacted", sa.Boolean(), server_default="0", nullable=False))


def downgrade():
    schema = op.get_context().opts.get("version_table_schema")
    with op.batch_alter_table("task_runs", schema=schema) as batch_op:
        batch_op.drop_column("args_redacted")
