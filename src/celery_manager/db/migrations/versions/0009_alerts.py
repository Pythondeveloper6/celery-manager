"""alerts: alert_channels, alert_rules, alert_incidents, alert_worker_sightings

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-28 11:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # New tables only; nothing existing is touched, so this is safe on a
    # live database.
    op.create_table(
        "alert_channels",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("config", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    schema=op.get_context().opts.get("version_table_schema"))
    op.create_table(
        "alert_rules",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("target", sa.String(length=255), nullable=False),
        sa.Column("params", sa.Text(), nullable=False),
        sa.Column("channel_ids", sa.Text(), nullable=False),
        sa.Column("quiet_period_minutes", sa.Integer(), nullable=False),
        sa.Column("notify_resolved", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_check_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    schema=op.get_context().opts.get("version_table_schema"))
    op.create_table(
        "alert_worker_sightings",
        sa.Column("worker", sa.String(length=255), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("worker"),
    schema=op.get_context().opts.get("version_table_schema"))
    # `open_key` is unique and NULL once an incident resolves: the
    # database, not process memory, guarantees one open incident per rule
    # and key however many evaluators run.
    op.create_table(
        "alert_incidents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("open_key", sa.String(length=300), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("delivery_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["rule_id"], [(op.get_context().opts.get("version_table_schema") + "." if op.get_context().opts.get("version_table_schema") else "") + "alert_rules.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("open_key"),
    schema=op.get_context().opts.get("version_table_schema"))
    with op.batch_alter_table("alert_incidents", schema=op.get_context().opts.get("version_table_schema")) as batch_op:
        batch_op.create_index(batch_op.f("ix_alert_incidents_opened_at"), ["opened_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_alert_incidents_rule_id"), ["rule_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_alert_incidents_state"), ["state"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("alert_incidents", schema=op.get_context().opts.get("version_table_schema")) as batch_op:
        batch_op.drop_index(batch_op.f("ix_alert_incidents_state"))
        batch_op.drop_index(batch_op.f("ix_alert_incidents_rule_id"))
        batch_op.drop_index(batch_op.f("ix_alert_incidents_opened_at"))

    op.drop_table("alert_incidents", schema=op.get_context().opts.get("version_table_schema"))
    op.drop_table("alert_worker_sightings", schema=op.get_context().opts.get("version_table_schema"))
    op.drop_table("alert_rules", schema=op.get_context().opts.get("version_table_schema"))
    op.drop_table("alert_channels", schema=op.get_context().opts.get("version_table_schema"))
