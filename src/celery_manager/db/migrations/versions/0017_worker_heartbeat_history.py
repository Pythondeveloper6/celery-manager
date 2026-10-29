"""Minute-bucket worker history with bounded retention."""
import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().opts.get("version_table_schema")
    op.create_table("worker_heartbeat_samples",
                    sa.Column("hostname", sa.String(255), primary_key=True),
                    sa.Column("bucket_at", sa.DateTime(timezone=True), primary_key=True),
                    sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("state", sa.String(16), nullable=False),
                    sa.Column("active", sa.Integer(), nullable=True),
                    sa.Column("processed", sa.Integer(), nullable=True), schema=schema)
    op.create_index("ix_worker_heartbeat_samples_observed_at", "worker_heartbeat_samples", ["observed_at"], schema=schema)


def downgrade():
    op.drop_table("worker_heartbeat_samples", schema=op.get_context().opts.get("version_table_schema"))
