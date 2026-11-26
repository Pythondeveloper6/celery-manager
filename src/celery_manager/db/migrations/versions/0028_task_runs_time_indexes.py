"""Time and composite indexes on task_runs."""
import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

INDEXES = {
    "ix_task_runs_finished_at": ["finished_at"],
    "ix_task_runs_started_at": ["started_at"],
    "ix_task_runs_task_name_finished_at": ["task_name", "finished_at"],
    "ix_task_runs_status_id": ["status", "id"],
    "ix_task_runs_queue_started_at": ["queue", "started_at"],
}


def upgrade():
    schema = op.get_context().opts.get("version_table_schema")
    # shortcut: plain CREATE INDEX blocks task_runs writes while it builds;
    # on a very large table build them online by hand first (docs/retention.md),
    # which is why an index that already exists is skipped.
    existing = {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes("task_runs", schema=schema)}
    for name, columns in INDEXES.items():
        if name not in existing:
            op.create_index(name, "task_runs", columns, schema=schema)


def downgrade():
    schema = op.get_context().opts.get("version_table_schema")
    for name in INDEXES:
        op.drop_index(name, table_name="task_runs", schema=schema)
