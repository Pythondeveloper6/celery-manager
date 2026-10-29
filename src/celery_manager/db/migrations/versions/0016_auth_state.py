"""Shared login backoff and individually revocable browser sessions."""
import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    schema = op.get_context().opts.get("version_table_schema")
    op.create_table("browser_sessions", sa.Column("session_id", sa.String(64), primary_key=True),
                    sa.Column("username", sa.String(255), nullable=False),
                    sa.Column("source", sa.String(16), nullable=False),
                    sa.Column("subject", sa.String(255), nullable=True),
                    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True), schema=schema)
    op.create_index("ix_browser_sessions_username", "browser_sessions", ["username"], schema=schema)
    op.create_index("ix_browser_sessions_expires_at", "browser_sessions", ["expires_at"], schema=schema)
    op.create_table("login_backoff", sa.Column("key", sa.String(64), primary_key=True),
                    sa.Column("attempts", sa.Integer(), nullable=False),
                    sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
                    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), schema=schema)
    op.create_index("ix_login_backoff_updated_at", "login_backoff", ["updated_at"], schema=schema)


def downgrade():
    schema = op.get_context().opts.get("version_table_schema")
    op.drop_table("login_backoff", schema=schema)
    op.drop_table("browser_sessions", schema=schema)
