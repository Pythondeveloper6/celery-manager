"""Use a UTC server default on SQL Server and MySQL."""
import sqlalchemy as sa
from alembic import op

from celery_manager.db.dialect import utcnow

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name in ("mssql", "mysql", "mariadb"):
        op.alter_column("task_runs", "updated_at", existing_type=sa.DateTime(timezone=True),
                        existing_nullable=False, existing_server_default=sa.func.now(),
                        server_default=utcnow(), schema=op.get_context().opts.get("version_table_schema"))


def downgrade():
    if op.get_bind().dialect.name in ("mssql", "mysql", "mariadb"):
        op.alter_column("task_runs", "updated_at", existing_type=sa.DateTime(timezone=True),
                        existing_nullable=False, existing_server_default=utcnow(),
                        server_default=sa.func.now(), schema=op.get_context().opts.get("version_table_schema"))
