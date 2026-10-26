from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TaskRun(Base):
    __tablename__ = "task_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    task_name: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)  # queued/started/succeeded/failed/retried/skipped/revoked
    queue: Mapped[str | None] = mapped_column(String(255), nullable=True)
    worker: Mapped[str | None] = mapped_column(String(255), nullable=True)
    args: Mapped[str] = mapped_column(Text, default="[]")
    kwargs: Mapped[str] = mapped_column(Text, default="{}")
    eta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    runtime_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    exception: Mapped[str | None] = mapped_column(Text, nullable=True)
    traceback: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    schedule_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    # Retry lineage. Set by `retry_task` on the *new* run it dispatches, so a
    # run can point back at the run it was retried from and the detail view
    # can show the whole chain in both directions (`list_retries_of` walks it
    # forwards). Deliberately a plain task_id string, not a ForeignKey to
    # task_runs.id: the parent row may not exist yet (or ever) when the child
    # is written — the events listener and the decorator both create rows
    # independently, and a retry can outlive a purged parent.
    retried_from_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    # Live progress, written by `celery_manager.report_progress()` from inside
    # a running task body. All four are nullable because progress is opt-in:
    # a task that never calls report_progress simply has none, which the UI
    # renders as "no progress reported" rather than as 0%.
    #
    # `progress_total` may be None for open-ended work ("processed 412 rows",
    # no known denominator), in which case there is no percentage to show —
    # only a count and whatever message the task supplied.
    progress_current: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Workflow lineage (chains, groups, chords), from Celery's own
    # `parent_id` / `root_id` / `group`. Plain task_id strings, not
    # ForeignKeys, for the same reason as `retried_from_id`: rows are created
    # independently and in any order by the listener and the decorator.
    parent_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    root_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    group_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)


class PeriodicSchedule(Base):
    __tablename__ = "periodic_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    task_name: Mapped[str] = mapped_column(String(255))
    schedule_spec: Mapped[str] = mapped_column(String(255))  # human-readable, e.g. "2h" or "crontab(minute=0,hour=7)"
    args: Mapped[str] = mapped_column(Text, default="[]")
    kwargs: Mapped[str] = mapped_column(Text, default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Whole-schedule pause. `paused_at IS NOT NULL` *is* the paused state —
    # there is no separate flag to drift out of step with it. While paused,
    # every firing of this schedule that reaches a `@track`ed task is
    # recorded as `skipped` and the body does not run (see
    # `celery_manager.decorator`). Deliberately separate from `enabled`,
    # which `sync_periodic_schedules` owns and rewrites on every startup:
    # an operator's pause must survive a restart. Added by migration 0002.
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paused_by: Mapped[str | None] = mapped_column(String(255), nullable=True)


class ScheduleOverride(Base):
    __tablename__ = "schedule_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    schedule_name: Mapped[str] = mapped_column(String(255), ForeignKey("periodic_schedules.name"), index=True)
    target_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    action: Mapped[str] = mapped_column(String(32))  # skip | cancel | reschedule
    reschedule_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))


class AppSetting(Base):
    """Deployment-wide dashboard settings, as a key/value table.

    Currently only branding (`brand_name`, `brand_accent`, `brand_logo_url`),
    and deliberately server-side rather than per-browser: branding is a
    property of the deployment — one operator sets it, everyone on the team
    sees the same dashboard. Per-*user* preferences that shouldn't be shared
    (the light/dark theme choice) stay in the browser's localStorage and
    never reach this table.

    Key/value rather than a one-row typed table so adding a setting later is
    a write, not a schema migration.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str] = mapped_column(String(255))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
