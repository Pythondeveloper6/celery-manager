"""Lease lifetime management for explicitly opted-in execution controls."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from threading import Event, Thread

from celery_manager.locks import SingletonLeaseLost

logger = logging.getLogger(__name__)


@contextmanager
def maintain_lease(provider, lease, ttl):
    stop, lost = Event(), Event()

    def renew():
        while not stop.wait(min(ttl / 3, 30)):
            try:
                if provider.renew(lease, ttl):
                    continue
            except Exception:
                logger.exception("singleton lease renewal failed for task %s", lease.task_id)
            lost.set()
            logger.critical("Singleton lease ownership lost for task %s; running body cannot be stopped safely", lease.task_id)
            return

    thread = Thread(target=renew, name="cm-singleton-renew", daemon=True)
    thread.start()
    try:
        yield
        # Verify ownership even for tasks finishing before the first renewal.
        try:
            owned = provider.renew(lease, ttl)
        except Exception:
            owned = False
            logger.exception("singleton final ownership verification failed")
        if lost.is_set() or not owned:
            raise SingletonLeaseLost("Singleton lease ownership lost; task side effects may have occurred")
    finally:
        stop.set()
        thread.join(timeout=0.1)
        try:
            if not provider.release(lease):
                logger.warning("singleton lease no longer owned by task %s during release", lease.task_id)
        except Exception:
            # Never replace an exception raised by the task, nor retry a
            # successful body solely because cleanup failed. TTL cleans up.
            logger.exception("singleton release failed for task %s; awaiting TTL", lease.task_id)
