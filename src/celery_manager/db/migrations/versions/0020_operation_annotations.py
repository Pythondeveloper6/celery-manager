"""Environment-aware deployment and planned-maintenance timeline."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import TEXT, VARCHAR

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().opts.get("version_table_schema")
    op.create_table("operation_annotations", sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
                    sa.Column("kind", sa.String(16), nullable=False), sa.Column("environment", sa.Unicode(100).with_variant(VARCHAR(100, charset="utf8mb4"), "mysql"), nullable=False),
                    sa.Column("label", sa.Unicode(255).with_variant(VARCHAR(255, charset="utf8mb4"), "mysql"), nullable=False), sa.Column("revision", sa.Unicode(255).with_variant(VARCHAR(255, charset="utf8mb4"), "mysql"), nullable=True),
                    sa.Column("note", sa.UnicodeText().with_variant(TEXT(charset="utf8mb4"), "mysql"), nullable=True), sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("actor", sa.String(255), nullable=False), sa.Column("actor_subject", sa.String(255), nullable=True), schema=schema)
    for field in ("environment", "starts_at"):
        op.create_index("ix_operation_annotations_" + field, "operation_annotations", [field], schema=schema)


def downgrade():
    op.drop_table("operation_annotations", schema=op.get_context().opts.get("version_table_schema"))
