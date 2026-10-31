"""Opt-in, audited preconditions for sensitive operator requests."""
from __future__ import annotations

import re
from urllib.parse import unquote

from fastapi import HTTPException

# Paths include the API prefix so the client and server use one contract.
SENSITIVE_ACTIONS = (
    ("POST", r"/api/tasks/start", "task.start"),
    ("POST", r"/api/tasks/(?:[^/]+/retry|retry-bulk)", "task.retry"),
    ("POST", r"/api/dead-letters/rerun", "task.retry"),
    ("POST", r"/api/tasks/(?:[^/]+/cancel|cancel-bulk)", "task.cancel"),
    ("POST", r"/api/delayed/cancel", "task.cancel"),
    ("POST", r"/api/locks/release", "lock.release"),
    ("POST", r"/api/queues/[^/]+/purge", "queue.purge"),
    ("DELETE", r"/api/queues/[^/]+/messages/[^/]+", "queue.delete"),
    ("POST", r"/api/queues/[^/]+/messages/[^/]+/move", "queue.move"),
    ("POST", r"/api/queues/unacked/restore", "queue.restore"),
    ("POST", r"/api/rabbitmq/dlqs/[^/]+/requeue", "queue.requeue"),
    ("POST", r"/api/workers/[^/]+/shutdown", "worker.shutdown"),
    ("DELETE", r"/api/schedules/[^/]+", "schedule.delete"),
    ("POST", r"/api/schedule-reconciliation/apply", "schedule.reconcile"),
    ("DELETE", r"/api/health/schedulers/[^/]+", "scheduler.retire"),
    ("DELETE", r"/api/health/beat/owner", "beat.retire"),
)


def action_contract():
    return [{"method": method, "path": path, "action": action} for method, path, action in SENSITIVE_ACTIONS]


def classify_action(method, path):
    path = path.replace("/api/v1/", "/api/", 1)
    return next((action for verb, pattern, action in SENSITIVE_ACTIONS
                 if method == verb and re.fullmatch(pattern, path)), None)


def enforce_sensitive_action(request, principal):
    config = request.app.state.cm_config
    if not config.operator_safeguards:
        return
    # Scope.path may include a mount prefix; root_path identifies that prefix.
    path, root = request.scope["path"], request.scope.get("root_path", "")
    if root and path.startswith(root):
        path = path[len(root):]
    action = classify_action(request.method, path)
    if action is None or getattr(request.state, "cm_guard_accepted", None) == action:
        return
    try:
        raw_reason = request.headers.get("x-cm-action-reason", "")
        if len(raw_reason) > 12000:
            raw_reason = ""
        reason = unquote(raw_reason, encoding="utf-8", errors="strict").strip()
        environment = unquote(request.headers.get("x-cm-confirm-environment", ""), encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        reason, environment = "", ""

    valid_reason = 1 <= len(reason) <= 500 and reason.isprintable()
    accepted = valid_reason and environment == config.environment_name
    from celery_manager.control.audit import record
    try:
        with request.app.state.cm_session_factory() as session:
            record(session, principal.username, "guardrail.accepted" if accepted else "guardrail.denied",
                   "operator_action", action,
                   {"environment": config.environment_name, "reason": reason if valid_reason else None,
                    "intent_only": True}, actor_subject=principal.subject)
    except Exception as exc:
        raise HTTPException(503, "The action audit could not be recorded. Nothing was dispatched.") from exc
    if not accepted:
        raise HTTPException(428, f"This action requires a printable reason (1–500 characters) and confirmation of environment {config.environment_name!r}.")
    request.state.cm_guard_accepted = action
