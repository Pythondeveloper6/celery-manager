from dataclasses import replace

from .base import ScheduleConflict, ScheduleEntry


def _same(a, b):
    # Beat rewrites last_run_at on every firing; that is not an edit.
    return replace(a, last_run_at=None) == replace(b, last_run_at=None)


class RedBeatBackend:
    name = "redbeat"
    writable = True
    checks_conflicts = True  # update/delete accept `expected=` (the entry as read)

    def __init__(self, celery_app):
        from redbeat.schedulers import RedBeatSchedulerEntry, ensure_conf, get_redis
        self.app = celery_app
        self.conf = ensure_conf(celery_app)
        self.redis = get_redis(celery_app)
        self.Entry = RedBeatSchedulerEntry

    def _convert(self, entry):
        return ScheduleEntry(entry.name, entry.task, entry.schedule, list(entry.args or []),
                             dict(entry.kwargs or {}), dict(entry.options or {}),
                             entry.enabled, entry.last_run_at, self.name)

    def list(self):
        entries = []
        # Includes disabled entries. Never scans/wipes the Redis keyspace.
        for key in self.redis.zrange(self.conf.schedule_key, 0, -1):
            try:
                entries.append(self._convert(self.Entry.from_key(key, app=self.app)))
            except KeyError:
                continue  # A concurrent deletion or orphaned zset member.
        return entries

    def get(self, name):
        try:
            return self._convert(self.Entry.from_key(self.conf.key_prefix + name, app=self.app))
        except KeyError:
            return None

    def _save(self, entry):
        saved = self.Entry(name=entry.name, task=entry.task, schedule=entry.schedule,
                           args=entry.args, kwargs=entry.kwargs, options=entry.options,
                           enabled=entry.enabled, app=self.app)
        saved.last_run_at = entry.last_run_at
        saved.save()
        return self._convert(saved)

    def _lock(self, name):
        # One key per entry for create, update and delete (the key name
        # predates update/delete taking it).
        return self.redis.lock(self.conf.key_prefix + "cm-create:" + name, timeout=30, blocking_timeout=10)

    def create(self, entry):
        with self._lock(entry.name):
            if self.get(entry.name) is not None:
                raise FileExistsError(entry.name)
            return self._save(entry)

    def _current(self, name, expected):
        entry = self.get(name)
        if entry is None:
            raise KeyError(name)
        if expected is not None and not _same(entry, expected):
            raise ScheduleConflict(name)
        return entry

    def update(self, name, expected=None, **changes):
        with self._lock(name):
            return self._save(replace(self._current(name, expected), **changes))

    def delete(self, name, expected=None):
        with self._lock(name):
            self._current(name, expected)
            self.Entry.from_key(self.conf.key_prefix + name, app=self.app).delete()

    def beat_scheduler_class(self):
        return "celery_manager.beat:RedBeatScheduler"
