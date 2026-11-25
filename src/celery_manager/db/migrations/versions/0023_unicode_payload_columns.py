"""Store free text as NVARCHAR on SQL Server; a no-op on every other dialect."""
import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

# (table, column, nullable). Text columns become NVARCHAR(max), the rest NVARCHAR(255).
TEXT = [
    ("alert_channels", "config", False), ("alert_channels", "last_error", True),
    ("alert_rules", "params", False), ("alert_rules", "channel_ids", False),
    ("alert_rules", "last_check_error", True), ("app_settings", "value", True),
    ("audit_log", "details", True), ("failure_resolution_history", "note", True),
    ("failure_resolutions", "note", True), ("maintenance_windows", "applies_to", False),
    ("periodic_schedules", "args", False), ("periodic_schedules", "kwargs", False),
    ("periodic_schedules", "definition", True), ("periodic_schedules", "schedule_override", True),
    ("task_runs", "context_json", True), ("task_runs", "args", False), ("task_runs", "kwargs", False),
    ("task_runs", "exception", True), ("task_runs", "traceback", True), ("task_runs", "log_output", True),
    ("task_runs", "result_repr", True), ("task_runs", "progress_message", True),
    ("alert_incidents", "details", True), ("alert_incidents", "delivery_error", True),
]
# Indexed labels: SQL Server cannot alter a column an index depends on, so the index is rebuilt.
INDEXED = [("llm_calls", "task_name", True), ("task_runs", "task_name", False),
           ("task_runs", "initiator", True), ("task_runs", "schedule_name", True)]
PLAIN = [("task_runs", "queue", True), ("task_runs", "worker", True), ("task_runs", "skip_reason", True)]


def _convert(to_unicode):
    schema = op.get_context().opts.get("version_table_schema")
    text_old, text_new = (sa.Text(), sa.Unicode()) if to_unicode else (sa.Unicode(), sa.Text())
    str_old, str_new = (sa.String(255), sa.Unicode(255)) if to_unicode else (sa.Unicode(255), sa.String(255))
    for table, column, nullable in INDEXED:
        op.drop_index(f"ix_{table}_{column}", table_name=table, schema=schema)
    for rows, old, new in ((TEXT, text_old, text_new), (INDEXED + PLAIN, str_old, str_new)):
        for table, column, nullable in rows:
            op.alter_column(table, column, schema=schema, existing_type=old, type_=new, existing_nullable=nullable)
    for table, column, nullable in INDEXED:
        op.create_index(f"ix_{table}_{column}", table, [column], schema=schema)


def upgrade():
    if op.get_bind().dialect.name == "mssql":
        _convert(True)


def downgrade():
    if op.get_bind().dialect.name == "mssql":
        _convert(False)
