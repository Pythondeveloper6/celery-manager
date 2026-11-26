"""schedule_firings (one row per beat send) and periodic_schedules.first_seen_at."""
import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().opts.get("version_table_schema")
    op.create_table(
        "schedule_firings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("schedule_name", sa.String(255).with_variant(sa.Unicode(255), "mssql"), nullable=False),
        sa.Column("occurrence_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("task_id", sa.String(255), nullable=True),
        schema=schema,
    )
    for column in ("schedule_name", "occurrence_at", "task_id"):
        op.create_index(f"ix_schedule_firings_{column}", "schedule_firings", [column], schema=schema)
    with op.batch_alter_table("periodic_schedules", schema=schema) as batch_op:
        batch_op.add_column(sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    schema = op.get_context().opts.get("version_table_schema")
    with op.batch_alter_table("periodic_schedules", schema=schema) as batch_op:
        batch_op.drop_column("first_seen_at")
    op.drop_table("schedule_firings", schema=schema)
