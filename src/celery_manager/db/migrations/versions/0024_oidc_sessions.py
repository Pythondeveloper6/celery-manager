"""browser_sessions: refresh token, role and role check time for OIDC sign-in."""
import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

_COLUMNS = (("refresh_token", sa.Text()), ("role", sa.String(16)), ("roles_checked_at", sa.DateTime(timezone=True)))


def upgrade():
    schema = op.get_context().opts.get("version_table_schema")
    with op.batch_alter_table("browser_sessions", schema=schema) as batch_op:
        for name, type_ in _COLUMNS:
            batch_op.add_column(sa.Column(name, type_, nullable=True))


def downgrade():
    schema = op.get_context().opts.get("version_table_schema")
    with op.batch_alter_table("browser_sessions", schema=schema) as batch_op:
        for name, _ in reversed(_COLUMNS):
            batch_op.drop_column(name)
