"""Runtime snapshots: with `inspect_source = "snapshot"` the events listener
process asks workers and the broker every `inspect_snapshot_seconds` and
stores the replies, so dashboard reads never wait on the broker (a mounted
dashboard in a synchronous WSGI host otherwise holds a request worker per
broker call)."""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from celery_manager.db.models import AppSetting

logger = logging.getLogger("celery_manager")
KEYS = {"workers": "runtime_snapshot:workers", "queues": "runtime_snapshot:queues"}


def _store(session, kind, value, now):
    row = session.get(AppSetting, KEYS[kind]) or AppSetting(key=KEYS[kind])
    row.value, row.updated_at = json.dumps(value, default=str), now
    session.add(row)
    session.commit()


def collect_once(config, session_factory) -> None:
    """Store one snapshot of each; a failing source keeps its last one."""
    from celery_manager.control.queues import list_queues
    from celery_manager.control.workers import inspect_workers
    now = datetime.now(timezone.utc)
    with session_factory() as session:
        try:
            _store(session, "workers", inspect_workers(config.celery_app, config.inspect_timeout_seconds), now)
        except Exception:
            session.rollback()
            logger.warning("celery-manager: worker snapshot failed", exc_info=True)
        try:
            _store(session, "queues", list_queues(config.celery_app, session), now)
        except Exception:
            session.rollback()
            logger.warning("celery-manager: queue snapshot failed", exc_info=True)


def read(session, kind):
    """`(value, taken_at)` of the last snapshot, or `(None, None)`."""
    row = session.get(AppSetting, KEYS[kind])
    if row is None or row.value is None:
        return None, None
    at = row.updated_at if row.updated_at.tzinfo else row.updated_at.replace(tzinfo=timezone.utc)
    return json.loads(row.value), at


def start_in_background(config, session_factory):
    """Run the collector on a daemon thread; a no-op unless
    `inspect_source == "snapshot"`."""
    if config.inspect_source != "snapshot" or config.celery_app is None:
        return None
    stop = threading.Event()

    def loop():
        while True:
            try:
                collect_once(config, session_factory)
            except Exception:
                logger.warning("celery-manager: snapshot collection failed", exc_info=True)
            if stop.wait(config.inspect_snapshot_seconds):
                return

    threading.Thread(target=loop, name="cm-snapshots", daemon=True).start()
    return stop
