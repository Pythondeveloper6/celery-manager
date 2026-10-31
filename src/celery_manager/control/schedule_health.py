"""Non-destructive drift diagnostics with explicitly bounded observations."""
from celery_manager.db.models import PeriodicSchedule
from celery_manager.schedulers.base import StaticBeatBackend
from celery_manager.schedulers.reconcile import plan_reconcile


def schedule_health(config, session, *, inspect_consumers=False):
    config.celery_app.loader.import_default_modules()
    service = config._schedule_service
    items = []
    for name, entry in service.entries.items():
        if entry["task"] not in config.celery_app.tasks:
            items.append({"name": name, "kind": "unregistered_task", "task": entry["task"]})
    for (name,) in session.query(PeriodicSchedule.name).filter(~PeriodicSchedule.name.in_(list(service.entries))).all():
        items.append({"name": name, "kind": "historical_entry_missing_from_backend"})
    drift_state = "known"
    try:
        plan = plan_reconcile(service.backend, StaticBeatBackend(config.celery_app).list())
        for change in plan["changes"]:
            items.append({"name": change["name"], "kind": "definition_drift", "difference": change["action"]})
    except (ValueError, TypeError):
        drift_state = "unknown"
    observation = {"state": "not_requested", "responding_workers": []}
    if inspect_consumers:
        try:
            replies = config.celery_app.control.inspect(timeout=config.inspect_timeout_seconds).active_queues()
            valid = isinstance(replies, dict) and bool(replies) and all(isinstance(v, list) for v in replies.values())
            if valid:
                observed = {queue["name"] for queues in replies.values() for queue in queues
                            if isinstance(queue, dict) and isinstance(queue.get("name"), str)}
                observation = {"state": "observed", "responding_workers": sorted(replies)}
                for name, entry in service.entries.items():
                    queue = entry.get("options", {}).get("queue") or config.celery_app.conf.task_default_queue
                    if queue and queue not in observed:
                        items.append({"name": name, "kind": "no_observed_consumer", "queue": queue})
            else:
                observation["state"] = "unknown"
        except Exception:  # noqa: BLE001 - broadcast failures are missing evidence, not no consumers
            observation["state"] = "unknown"
    return {"items": items, "backend": service.backend.name, "definition_drift": drift_state,
            "consumers": observation}
