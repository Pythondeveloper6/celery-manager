"""Durable worker liveness from events."""
import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("worker_states", sa.Column("hostname", sa.String(255), primary_key=True),
                    sa.Column("state", sa.String(16), nullable=False),
                    sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("active", sa.Integer(), nullable=True),
                    sa.Column("processed", sa.Integer(), nullable=True),
                    schema=op.get_context().opts.get("version_table_schema"))
    op.create_index("ix_worker_states_last_seen_at", "worker_states", ["last_seen_at"],
                    schema=op.get_context().opts.get("version_table_schema"))


def downgrade():
    op.drop_table("worker_states", schema=op.get_context().opts.get("version_table_schema"))
