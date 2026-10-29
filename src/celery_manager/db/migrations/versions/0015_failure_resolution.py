"""Failure acknowledgement and immutable workflow history."""
import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().opts.get("version_table_schema")
    op.create_table("failure_resolutions", sa.Column("task_id", sa.String(255), primary_key=True),
                    sa.Column("state", sa.String(16), nullable=False), sa.Column("actor", sa.String(255), nullable=False),
                    sa.Column("note", sa.Text(), nullable=True), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), schema=schema)
    op.create_table("failure_resolution_history", sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
                    sa.Column("task_id", sa.String(255), nullable=False), sa.Column("state", sa.String(16), nullable=False),
                    sa.Column("actor", sa.String(255), nullable=False), sa.Column("actor_subject", sa.String(255), nullable=True),
                    sa.Column("note", sa.Text(), nullable=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), schema=schema)
    op.create_index("ix_failure_resolution_history_task_id", "failure_resolution_history", ["task_id"], schema=schema)


def downgrade():
    schema = op.get_context().opts.get("version_table_schema")
    op.drop_table("failure_resolution_history", schema=schema)
    op.drop_table("failure_resolutions", schema=schema)
