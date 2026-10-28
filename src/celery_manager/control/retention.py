"""Data retention: delete old runs (and, optionally, old audit rows).

The rules live in the existing key/value `app_settings` table as one JSON
document, so this feature needs no schema migration. They are deliberately
*not* part of `control.settings.DEFAULT_SETTINGS`: that dict is served to
anonymous visitors by `GET /api/settings`, and which tasks a deployment prunes
(and for how long) has no business on the login screen.

Defaults delete nothing. A deployment that upgrades without touching the
settings keeps every row it had; pruning only ever starts after an admin sets
`default_days` or a per-task override.

What counts as "old", and what is never deleted
-----------------------------------------------
A run's age is `finished_at`. A row that never finished (the worker died, the
event was lost) has no `finished_at`, so its age is `updated_at`, the last time
anything touched it. On top of that:

- A `queued` or `started` row is only deleted once it has gone a *whole window*
  without any update, so a run that is merely waiting or still working is never
  pruned from under its worker. Progress reports bump `updated_at`.
- A run whose `eta` is still in the future is never deleted, however old the
  row is: it has not happened yet.
- With `failed_days` set, failed runs are kept for at least that long (the
  longer of it and the task's own window). Runs retried *from* such a failed
  run are kept as long as it is, otherwise the failure would reappear in the
  dead-letter view the moment its retry was pruned (a dead letter is a failed
  run nothing was retried from).

Lineage: `retried_from_id`, `parent_id`, `root_id` and `group_id` are plain
strings, not foreign keys, and every reader already tolerates the row they
name being gone (the retry list and workflow tree filter by the string; the
workflow edges only join ids that are present). Pruning therefore needs no
cascade and leaves no broken page behind.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, exists, func, or_
from sqlalchemy.orm import aliased

from celery_manager.control.audit import record as record_audit
from celery_manager.db.models import AppSetting, AuditLog, TaskRun

RETENTION_KEY = "retention"
LAST_PRUNE_KEY = "retention_last_prune"

MAX_DAYS = 36_500  # 100 years: a typo guard, not a policy
MAX_TASK_OVERRIDES = 500
MAX_TASK_NAME_LENGTH = 255  # task_runs.task_name is String(255)
DEFAULT_BATCH_SIZE = 1000
MAX_BATCH_SIZE = 10_000
# How many per-task counts an audit row / the last-prune record keeps. A
# deployment with thousands of task names would otherwise write a huge row on
# every prune.
_DETAIL_TASKS = 25

_ACTIVE = ("queued", "started")


@dataclass
class RetentionRules:
    """`None` means keep forever for every field."""

    default_days: int | None = None
    failed_days: int | None = None
    audit_days: int | None = None
    # task name -> days, or None to keep that task forever whatever the default says.
    tasks: dict[str, int | None] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "default_days": self.default_days,
            "failed_days": self.failed_days,
            "audit_days": self.audit_days,
            "tasks": dict(sorted(self.tasks.items())),
        }

    def days_for(self, task_name: str) -> int | None:
        if task_name in self.tasks:
            return self.tasks[task_name]
        return self.default_days


# --- validation ------------------------------------------------------------


def _validate_days(key: str, value: object) -> int | None:
    if value is None:
        return None
    # bool is an int subclass; `true` is not a number of days.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be a whole number of days or null, got {type(value).__name__}")  # noqa: TRY004 (ValueError contract)
    if not 1 <= value <= MAX_DAYS:
        raise ValueError(f"{key} must be between 1 and {MAX_DAYS} days (use null to keep forever)")
    return value


def _validate_tasks(value: object) -> dict[str, int | None]:
    if not isinstance(value, dict):
        raise ValueError("tasks must be an object of task name to days")  # noqa: TRY004
    if len(value) > MAX_TASK_OVERRIDES:
        raise ValueError(f"tasks can hold at most {MAX_TASK_OVERRIDES} overrides")
    cleaned: dict[str, int | None] = {}
    for name, days in value.items():
        if not isinstance(name, str) or not name.strip() or len(name) > MAX_TASK_NAME_LENGTH:
            raise ValueError(f"task names must be 1-{MAX_TASK_NAME_LENGTH} characters")
        cleaned[name] = _validate_days(f"tasks[{name}]", days)
    return cleaned


_FIELD_VALIDATORS = {
    "default_days": lambda v: _validate_days("default_days", v),
    "failed_days": lambda v: _validate_days("failed_days", v),
    "audit_days": lambda v: _validate_days("audit_days", v),
    "tasks": _validate_tasks,
}


def _check_consistent(rules: RetentionRules) -> None:
    # failed_days only ever lengthens retention (see the module docstring), so a
    # value shorter than the default would be silently inert. Say so instead.
    if (
        rules.default_days is not None
        and rules.failed_days is not None
        and rules.failed_days < rules.default_days
    ):
        raise ValueError("failed_days must be at least default_days: it keeps failed runs longer, never shorter")


# --- stored rules ----------------------------------------------------------


def get_retention(session) -> RetentionRules:
    """The stored rules, or keep-everything defaults. A damaged stored value
    reads as the defaults rather than raising: the safe failure for a
    deletion setting is to delete nothing."""
    row = session.get(AppSetting, RETENTION_KEY)
    if row is None or not row.value:
        return RetentionRules()
    try:
        data = json.loads(row.value)
        return RetentionRules(
            default_days=_validate_days("default_days", data.get("default_days")),
            failed_days=_validate_days("failed_days", data.get("failed_days")),
            audit_days=_validate_days("audit_days", data.get("audit_days")),
            tasks=_validate_tasks(data.get("tasks") or {}),
        )
    except (ValueError, AttributeError):
        return RetentionRules()


def merge_rules(current: RetentionRules, values: dict) -> RetentionRules:
    """`current` with the keys in `values` replaced (`tasks` whole), validated.
    Shared by the save path and the preview, so a preview judges exactly what
    a save would store."""
    if not isinstance(values, dict):
        raise ValueError("retention payload must be an object")  # noqa: TRY004 (ValueError contract)
    unknown = sorted(set(values) - set(_FIELD_VALIDATORS))
    if unknown:
        raise ValueError(f"unknown retention setting(s): {', '.join(unknown)}")
    merged = current.to_dict()
    for key, validate in _FIELD_VALIDATORS.items():
        if key in values:
            merged[key] = validate(values[key])
    rules = RetentionRules(**merged)
    _check_consistent(rules)
    return rules


def update_retention(session, values: dict, username: str) -> RetentionRules:
    """Partial update: only the keys in `values` change (`tasks` is replaced
    whole). Validated entirely before anything is written. Audited as one
    `update` entry when something actually changed."""
    current = get_retention(session)
    new = merge_rules(current, values)

    old_dict, new_dict = current.to_dict(), new.to_dict()
    if old_dict == new_dict:
        return current

    row = session.get(AppSetting, RETENTION_KEY)
    if row is None:
        row = AppSetting(key=RETENTION_KEY)
        session.add(row)
    row.value = json.dumps(new_dict, sort_keys=True)
    row.updated_at = datetime.now(timezone.utc)
    row.updated_by = username
    session.commit()
    record_audit(session, username, "update", "retention", RETENTION_KEY, {"old": old_dict, "new": new_dict})
    return new


def get_last_prune(session) -> dict | None:
    row = session.get(AppSetting, LAST_PRUNE_KEY)
    if row is None or not row.value:
        return None
    try:
        return json.loads(row.value)
    except ValueError:
        return None


# --- pruning ---------------------------------------------------------------


def _run_filter(task_name: str, days: int, failed_days: int | None, now: datetime):
    """SQL condition for the runs of one task that may be deleted now."""
    cutoff = now - timedelta(days=days)
    # failed_days only lengthens: a failed run waits for the longer of the two.
    failed_extended = failed_days is not None and failed_days > days
    failed_cutoff = now - timedelta(days=max(days, failed_days or 0))
    age = func.coalesce(TaskRun.finished_at, TaskRun.updated_at)

    conditions = [
        TaskRun.task_name == task_name,
        or_(
            and_(TaskRun.status != "failed", age < cutoff),
            and_(TaskRun.status == "failed", age < failed_cutoff),
        ),
        # Never prune live work: queued/started rows must also be idle for the window.
        or_(TaskRun.status.notin_(_ACTIVE), TaskRun.updated_at < cutoff),
        # A run scheduled for the future has not happened yet.
        or_(TaskRun.eta.is_(None), TaskRun.eta < now),
    ]
    if failed_extended:
        # Keep a retry for as long as the failed run it came from is kept, or
        # the original would turn back into a dead letter.
        parent = aliased(TaskRun)
        conditions.append(
            ~exists().where(parent.task_id == TaskRun.retried_from_id, parent.status == "failed").correlate(TaskRun)
        )
    return and_(*conditions)


def _delete_in_batches(session, model, condition, batch_size: int) -> int:
    deleted = 0
    while True:
        ids = [r[0] for r in session.query(model.id).filter(condition).order_by(model.id).limit(batch_size).all()]
        if not ids:
            break
        # One short transaction per batch, so a big first prune never holds the
        # table locked for its whole duration.
        session.execute(delete(model).where(model.id.in_(ids)))
        session.commit()
        deleted += len(ids)
        if len(ids) < batch_size:
            break
    return deleted


def _check_batch_size(batch_size: int) -> int:
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    return batch_size


def prune_runs(
    session,
    now: datetime | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    rules: RetentionRules | None = None,
) -> dict[str, int]:
    """Delete (or with `dry_run`, only count) runs past their retention.

    Returns `{task_name: rows deleted}` for tasks where at least one row was.
    Rules come from `app_settings` unless `rules` is given (the preview
    endpoint passes a candidate). Nothing is deleted when no rule applies.
    """
    _check_batch_size(batch_size)
    now = now or datetime.now(timezone.utc)
    rules = rules or get_retention(session)

    if rules.default_days is None:
        names = [n for n, d in rules.tasks.items() if d is not None]
    else:
        # Every task with rows; a task whose override is None (keep forever)
        # is skipped in the loop through days_for.
        names = [r[0] for r in session.query(TaskRun.task_name).distinct().all()]

    result: dict[str, int] = {}
    for name in sorted(names):
        days = rules.days_for(name)
        if days is None:
            continue
        condition = _run_filter(name, days, rules.failed_days, now)
        if dry_run:
            count = session.query(func.count(TaskRun.id)).filter(condition).scalar() or 0
        else:
            count = _delete_in_batches(session, TaskRun, condition, batch_size)
        if count:
            result[name] = count
    return result


def prune_audit_log(
    session,
    now: datetime | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    rules: RetentionRules | None = None,
) -> int:
    """Delete audit rows older than `audit_days`. 0 (and no query) when unset."""
    _check_batch_size(batch_size)
    rules = rules or get_retention(session)
    if rules.audit_days is None:
        return 0
    now = now or datetime.now(timezone.utc)
    condition = AuditLog.created_at < now - timedelta(days=rules.audit_days)
    if dry_run:
        return session.query(func.count(AuditLog.id)).filter(condition).scalar() or 0
    return _delete_in_batches(session, AuditLog, condition, batch_size)


def run_prune(
    session,
    *,
    username: str = "system",
    now: datetime | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    rules: RetentionRules | None = None,
) -> dict:
    """Prune runs and the audit log, then record what happened.

    A real prune stores a "last prune" record (shown on the Settings page) and
    writes one audit row *when something was deleted*: a prune scheduled hourly
    that finds nothing to do would otherwise bury the real entries. A dry run
    changes nothing and records nothing.
    """
    now = now or datetime.now(timezone.utc)
    by_task = prune_runs(session, now=now, batch_size=batch_size, dry_run=dry_run, rules=rules)
    audit_deleted = prune_audit_log(session, now=now, batch_size=batch_size, dry_run=dry_run, rules=rules)
    summary = {
        "dry_run": dry_run,
        "at": now.isoformat(),
        "runs_deleted": sum(by_task.values()),
        "audit_deleted": audit_deleted,
        "by_task": by_task,
    }
    if dry_run:
        return summary

    top = dict(sorted(by_task.items(), key=lambda kv: -kv[1])[:_DETAIL_TASKS])
    stored = {**summary, "by_task": top, "by": username, "tasks_truncated": len(by_task) > len(top)}
    row = session.get(AppSetting, LAST_PRUNE_KEY)
    if row is None:
        row = AppSetting(key=LAST_PRUNE_KEY)
        session.add(row)
    row.value = json.dumps(stored)
    row.updated_at = now
    row.updated_by = username
    session.commit()
    if summary["runs_deleted"] or audit_deleted:
        record_audit(
            session, username, "prune", "retention", "task_runs",
            {"runs_deleted": summary["runs_deleted"], "audit_deleted": audit_deleted, "by_task": top},
        )
    return summary
