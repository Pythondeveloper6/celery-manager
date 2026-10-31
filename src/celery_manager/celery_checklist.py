"""Celery configuration review: the settings behind most lost or stuck tasks.

Pure reads of `app.conf`; nothing connects anywhere. Each finding says what
was seen, whether it is the safer production choice, and the change to make
when it is not. These are recommendations, not errors: a deployment can have
good reasons for any of them (for example `task_acks_late=False` for tasks
that must never run twice), so `doctor` reports them as `info`, never `fail`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

# Redis/SQS redeliver an unacknowledged message after this many seconds.
_DEFAULT_VISIBILITY_TIMEOUT = 3600


@dataclass(frozen=True)
class Finding:
    name: str
    ok: bool
    detail: str
    fix: str | None = None

    def as_check(self) -> dict:
        check = asdict(self)
        ok = check.pop("ok")
        check["state"] = "pass" if ok else "info"
        if ok:
            check.pop("fix")
        return {"name": "celery_" + check.pop("name"), **check}


def _transport(app) -> str:
    url = str(app.conf.broker_url or "")
    return url.split("://", 1)[0].split("+", 1)[0].lower() if "://" in url else ""


def review(app) -> list[Finding]:
    conf = app.conf
    findings: list[Finding] = []
    acks_late = bool(conf.task_acks_late)
    findings.append(Finding(
        "acks_late", acks_late,
        f"task_acks_late={acks_late}",
        None if acks_late else
        "Set task_acks_late=True (and task_reject_on_worker_lost=True) for idempotent tasks, so a task "
        "killed mid-run is redelivered instead of lost. Keep it False only for tasks that must never run twice.",
    ))
    if acks_late:
        lost = bool(conf.task_reject_on_worker_lost)
        findings.append(Finding(
            "reject_on_worker_lost", lost, f"task_reject_on_worker_lost={lost}",
            None if lost else "With acks_late, also set task_reject_on_worker_lost=True so a worker killed by "
            "the OOM killer or SIGKILL requeues its task.",
        ))
    prefetch = conf.worker_prefetch_multiplier
    findings.append(Finding(
        "prefetch_multiplier", prefetch == 1 or not acks_late,
        f"worker_prefetch_multiplier={prefetch}",
        None if prefetch == 1 or not acks_late else
        "Set worker_prefetch_multiplier=1 for long-running tasks: with acks_late, prefetched messages wait "
        "behind a slow task on one worker while others sit idle.",
    ))
    transport = _transport(app)
    if transport in ("redis", "rediss", "sqs"):
        options = conf.broker_transport_options or {}
        visibility = options.get("visibility_timeout", _DEFAULT_VISIBILITY_TIMEOUT)
        limit = conf.task_time_limit
        ok = limit is None or visibility > limit
        findings.append(Finding(
            "visibility_timeout", ok,
            f"broker_transport_options.visibility_timeout={visibility}s, task_time_limit={limit}",
            None if ok else
            "Raise broker_transport_options['visibility_timeout'] above your longest task_time_limit, ETA "
            "and countdown: otherwise the broker redelivers a still-running task and it runs twice.",
        ))
        findings.append(Finding(
            "visibility_timeout_eta", True,
            f"visibility_timeout={visibility}s",
            None,
        ) if visibility >= 86400 else Finding(
            "visibility_timeout_eta", False,
            f"visibility_timeout={visibility}s",
            "Tasks scheduled further ahead than the visibility timeout (ETA/countdown) are redelivered and "
            "run more than once; keep long delays out of the broker or raise the timeout.",
        ))
    expires = conf.result_expires
    ok = expires is not None and expires != 0
    findings.append(Finding(
        "result_expires", ok, f"result_expires={expires}",
        None if ok else "Set result_expires (default one day) so the result backend does not grow without bound.",
    ))
    limits = conf.task_time_limit is not None or conf.task_soft_time_limit is not None
    findings.append(Finding(
        "time_limits", limits,
        f"task_time_limit={conf.task_time_limit}, task_soft_time_limit={conf.task_soft_time_limit}",
        None if limits else "Set task_soft_time_limit and task_time_limit (globally or per task) so a hung "
        "task frees its worker slot instead of blocking it forever.",
    ))
    events = bool(conf.worker_send_task_events) and bool(conf.task_send_sent_event)
    findings.append(Finding(
        "task_events", events,
        f"worker_send_task_events={conf.worker_send_task_events}, task_send_sent_event={conf.task_send_sent_event}",
        None if events else "Set worker_send_task_events=True and task_send_sent_event=True so the events "
        "listener records every task and its queue wait (`celery-manager events` can also enable them at runtime).",
    ))
    started = bool(conf.task_track_started)
    findings.append(Finding(
        "track_started", started, f"task_track_started={started}",
        None if started else "Set task_track_started=True so result-backend consumers can tell a running "
        "task from a queued one.",
    ))
    retry_on_startup = conf.broker_connection_retry_on_startup
    findings.append(Finding(
        "connection_retry_on_startup", retry_on_startup is not None,
        f"broker_connection_retry_on_startup={retry_on_startup}",
        None if retry_on_startup is not None else "Set broker_connection_retry_on_startup explicitly; Celery 5.3+ "
        "warns and its default will change.",
    ))
    findings.append(Finding(
        "single_beat", False, "beat must run as exactly one process per schedule set",
        "Run exactly one beat process (or a scheduler with a lock, such as RedBeat); two beats send every "
        "periodic task twice. celery-manager's beat-liveness owners show each running beat.",
    ))
    return findings


def render_checklist(report: dict) -> str:
    """A paste-ready Markdown checklist from a `doctor` report."""
    lines = [f"# celery-manager production checklist ({report.get('environment') or 'unnamed environment'})",
             "", f"Checked at {report.get('checked_at')}.", ""]
    for check in report["checks"]:
        done = check["state"] == "pass"
        lines.append(f"- [{'x' if done else ' '}] **{check['name']}**: {check['detail']}")
        if check.get("fix"):
            lines.append(f"  - Fix: {check['fix']}")
    return "\n".join(lines) + "\n"
