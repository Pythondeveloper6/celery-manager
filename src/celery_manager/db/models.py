from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from celery_manager.db.dialect import utcnow


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    # Queue wait inputs (migration 0011), from the task-sent / task-received
    # event timestamps. Earliest value wins; see events/listener.py and
    # control/latency.py. NULL when the event was never seen.
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    runtime_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    exception: Mapped[str | None] = mapped_column(Text, nullable=True)
    traceback: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_output_truncated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    result_repr: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_truncated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    initiator: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
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
    # Occurrence identity, from the headers the beat scheduler mixin stamps
    # (`celery_manager.beat`). `occurrence_at` is the scheduled time of the
    # firing this run belongs to; `beat_sent_at` is when beat sent it, the
    # anchor for projecting an interval schedule's next occurrences. Both
    # NULL for runs not sent by a stamping beat (manual calls, old beats).
    # Added by migration 0006.
    occurrence_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    beat_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Why a `skipped` run was skipped, in words the dashboard shows as-is
    # ("schedule paused", "maintenance window: Sunday patching"). NULL for
    # every other status and for skips recorded before migration 0010.
    skip_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # When anything on this row last changed — the dashboard's change feed.
    # The API process polls `updated_at > cursor` (see `db/changefeed.py`) to
    # tell connected browsers about writes made by *other* processes (the
    # events listener, workers), which its in-memory EventBus cannot hear.
    # Every writer gets this for free: `default` covers the Core
    # insert-if-absent in `db.runs.lock_task_run`, `onupdate` covers the ORM
    # flush of whatever each writer merges into the row afterwards. The
    # server default only serves rows inserted from outside this library.
    # Added by migration 0005.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        server_default=utcnow(),
        index=True,
    )


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
    # Optional auto-resume time (migration 0003). With `paused_at` set, the
    # schedule is paused only while `paused_until` is NULL (indefinite) or
    # still in the future; no job clears it — readers compute it and lazily
    # reset the stale pause (`control.schedules.expire_stale_pause`).
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # An operator's edit of when this schedule runs (migration 0008): a spec
    # as canonical JSON (`celery_manager.schedule_specs`), or NULL to run as
    # the code's `beat_schedule` says. Like the pause, an operator decision
    # `sync_periodic_schedules` never rewrites; `schedule_spec` above keeps
    # describing the code's schedule, so "revert to code" has something to
    # show. Beat applies it only when it runs
    # `celery_manager.beat:DatabaseScheduler`.
    definition: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    schedule_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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


class MaintenanceWindow(Base):
    """A recurring window during which chosen schedules' firings are skipped
    (migration 0010). Nothing is paused and nothing needs resuming: the
    decorator asks "is this occurrence inside an enabled window?" on every
    firing (`celery_manager.control.maintenance`), so beat keeps sending.

    The recurrence is a weekly one: `days_of_week` (CSV of 0-6, Monday = 0)
    each start at `start_time` ("HH:MM") in `timezone` (an IANA name) and last
    `duration_minutes` of elapsed time, so a window may cross midnight.
    `applies_to` is JSON: the string "*" or a list of schedule names.
    """

    __tablename__ = "maintenance_windows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    days_of_week: Mapped[str] = mapped_column(String(32))
    start_time: Mapped[str] = mapped_column(String(5))
    duration_minutes: Mapped[int] = mapped_column(Integer)
    timezone: Mapped[str] = mapped_column(String(64))
    applies_to: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QueueDepthSample(Base):
    """One reading of a queue's broker depth (migration 0011), taken by the
    sampler thread of the events listener (`control.depth_history`). Only
    depths the broker actually reported are stored, never guesses."""

    __tablename__ = "queue_depth_samples"
    __table_args__ = (Index("ix_queue_depth_samples_queue_sampled_at", "queue", "sampled_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    queue: Mapped[str] = mapped_column(String(255))
    depth: Mapped[int] = mapped_column(Integer)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # viewer | operator | admin (see celery_manager.auth.roles). The server
    # default is the least privileged role, so a row inserted from outside
    # this library can read but not act; `create_user()` passes its own
    # value. Accounts that existed before roles were upgraded to `admin` by
    # migration 0006, since that is what every account could do then.
    role: Mapped[str] = mapped_column(String(16), default="viewer", server_default="viewer")


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
    actor_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str] = mapped_column(String(255))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AlertChannel(Base):
    """Where alert messages go: a Slack incoming webhook, a generic webhook
    or a list of email addresses (see `celery_manager.alerts`).

    `config` is JSON whose shape depends on `kind`. It holds the webhook URL
    and the optional signing secret, so the API never returns it as stored
    (`control.alerts.channel_entry` masks it). SMTP credentials are *not*
    here: they come from `CeleryManagerConfig`, so a database dump or a
    viewer-readable API response can never leak a mail password.
    """

    __tablename__ = "alert_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[str] = mapped_column(String(16))  # slack | webhook | email
    config: Mapped[str] = mapped_column(Text, default="{}")
    created_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # Outcome of the most recent delivery (an alert or a test), for the
    # dashboard: an admin sees a broken channel before an incident does.
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AlertRule(Base):
    """One condition to watch, and who to tell when it holds.

    `target` is a task, queue, worker or schedule name for `kind`, or a
    glob (`*` for all); each matching name is judged on its own and gets its
    own incident. `params` is JSON with the kind's thresholds, validated
    and filled with defaults by `control.alerts`. `channel_ids` is a JSON
    list rather than a join table: rules have a handful of channels, and
    deleting a channel rewrites the few rules that name it.
    """

    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[str] = mapped_column(String(32))  # failure_rate | queue_backlog | worker_offline | missed_run
    target: Mapped[str] = mapped_column(String(255), default="*")
    params: Mapped[str] = mapped_column(Text, default="{}")
    channel_ids: Mapped[str] = mapped_column(Text, default="[]")
    quiet_period_minutes: Mapped[int] = mapped_column(Integer, default=60)
    notify_resolved: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the evaluator last judged this rule, and why it could not, if it
    # could not (broker unreachable, no worker replies...). A rule that
    # can't be checked neither fires nor resolves; the dashboard says so.
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_check_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AlertIncident(Base):
    """One stretch of time a rule held for one key (a task, queue, worker or
    schedule name). The quiet period and cross-process deduplication both
    live in this table rather than in memory, so any number of evaluators
    can run against one database:

    - `open_key` is `"<rule id>:<key>"` while the incident is firing and
      NULL once resolved. It is unique, so of two evaluators that see the
      same new problem at once only one inserts the incident (NULLs never
      collide, so resolved history is unconstrained).
    - `notified_at` is claimed with a compare-and-set UPDATE (`WHERE
      notified_at IS NULL`) before the message is sent, so exactly one
      process sends it. It stays NULL while the quiet period holds the
      message back.
    - `resolved_notified_at` likewise for the "resolved" message.
    """

    __tablename__ = "alert_incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(Integer, ForeignKey("alert_rules.id"), index=True)
    key: Mapped[str] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(16), index=True)  # firing | resolved
    open_key: Mapped[str | None] = mapped_column(String(300), nullable=True, unique=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Why the last message for this incident failed on some channel, if it did.
    delivery_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AlertWorkerSighting(Base):
    """When the alerts evaluator last saw each worker answer a ping.

    `worker_offline` rules need to know which workers *should* be there,
    and a ping only lists the ones that are. Every worker the evaluator has
    seen is remembered here; one that stops answering for longer than the
    rule's threshold is offline. A worker gone for longer than
    `alerts.WORKER_FORGET_AFTER` is forgotten (scaled down on purpose), which
    resolves its incident.
    """

    __tablename__ = "alert_worker_sightings"

    worker: Mapped[str] = mapped_column(String(255), primary_key=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WorkerState(Base):
    __tablename__ = "worker_states"
    hostname: Mapped[str] = mapped_column(String(255), primary_key=True)
    state: Mapped[str] = mapped_column(String(16))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    active: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processed: Mapped[int | None] = mapped_column(Integer, nullable=True)

# Register separately defined workflow tables on the same metadata.
from celery_manager.db.auth_state import BrowserSession, LoginBackoff  # noqa: F401
from celery_manager.db.failures import FailureResolution, FailureResolutionHistory  # noqa: F401
from celery_manager.db.worker_history import WorkerHeartbeatSample  # noqa: F401
