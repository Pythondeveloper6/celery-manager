"""users.role: viewer / operator / admin

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-27 09:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Before this revision every account could do everything, so every
    # existing account becomes `admin`: an upgrade must never lock anyone out
    # of a control they had yesterday. Added nullable, backfilled, then
    # tightened, so the backfill value (admin) can differ from the default
    # for new rows (viewer, the least privileged role).
    with op.batch_alter_table("users", schema=op.get_context().opts.get("version_table_schema")) as batch_op:
        batch_op.add_column(sa.Column("role", sa.String(length=16), nullable=True))

    table = sa.table("users", sa.column("role", sa.String(16)), schema=op.get_context().opts.get("version_table_schema"))
    op.execute(table.update().values(role="admin"))

    with op.batch_alter_table("users", schema=op.get_context().opts.get("version_table_schema")) as batch_op:
        batch_op.alter_column(
            "role",
            existing_type=sa.String(length=16),
            nullable=False,
            server_default="viewer",
        )


def downgrade() -> None:
    with op.batch_alter_table("users", schema=op.get_context().opts.get("version_table_schema")) as batch_op:
        batch_op.drop_column("role")
