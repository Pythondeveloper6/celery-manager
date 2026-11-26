"""external_calls: outbound HTTP calls made inside tracked runs."""
import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().opts.get("version_table_schema")
    op.create_table(
        "external_calls",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(255), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("target", sa.String(255).with_variant(sa.Unicode(255), "mssql"), nullable=False),
        sa.Column("method", sa.String(16), nullable=True),
        sa.Column("status", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("error", sa.String(128), nullable=True),
        sa.Column("bytes", sa.Integer(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        schema=schema,
    )
    for column in ("task_id", "target", "at"):
        op.create_index(f"ix_external_calls_{column}", "external_calls", [column], schema=schema)


def downgrade():
    op.drop_table("external_calls", schema=op.get_context().opts.get("version_table_schema"))
