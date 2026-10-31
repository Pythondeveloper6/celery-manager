"""Durable custom probes and expected external scheduler commands."""
import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().opts.get("version_table_schema")
    op.create_table("health_observations", sa.Column("name", sa.String(64), primary_key=True),
                    sa.Column("state", sa.String(16), nullable=False), sa.Column("metrics", sa.Text(), nullable=False),
                    sa.Column("reason", sa.String(32), nullable=True),
                    sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False), schema=schema)
    op.create_index("ix_health_observations_observed_at", "health_observations", ["observed_at"], schema=schema)
    op.create_table("scheduler_instances", sa.Column("instance_id", sa.String(128), primary_key=True),
                    sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True), schema=schema)
    op.create_table("expected_scheduler_commands", sa.Column("instance_id", sa.String(128), primary_key=True),
                    sa.Column("name", sa.String(128), primary_key=True),
                    sa.Column("schedule_spec", sa.Text(), nullable=False), sa.Column("grace_seconds", sa.Float(), nullable=False),
                    sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True), schema=schema)


def downgrade():
    schema = op.get_context().opts.get("version_table_schema")
    op.drop_table("expected_scheduler_commands", schema=schema)
    op.drop_table("scheduler_instances", schema=schema)
    op.drop_table("health_observations", schema=schema)
