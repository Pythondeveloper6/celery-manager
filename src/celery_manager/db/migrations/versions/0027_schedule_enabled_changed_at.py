"""periodic_schedules.enabled_changed_at: when a schedule was last re-enabled."""
import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().opts.get("version_table_schema")
    with op.batch_alter_table("periodic_schedules", schema=schema) as batch_op:
        batch_op.add_column(sa.Column("enabled_changed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    schema = op.get_context().opts.get("version_table_schema")
    with op.batch_alter_table("periodic_schedules", schema=schema) as batch_op:
        batch_op.drop_column("enabled_changed_at")
