from dataclasses import replace

from .base import ScheduleEntry


class RedBeatBackend:
    name = "redbeat"
    writable = True

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

    def create(self, entry):
        with self.redis.lock(self.conf.key_prefix + "cm-create:" + entry.name, timeout=30, blocking_timeout=10):
            if self.get(entry.name) is not None:
                raise FileExistsError(entry.name)
            return self._save(entry)

    def update(self, name, **changes):
        entry = self.get(name)
        if entry is None:
            raise KeyError(name)
        return self._save(replace(entry, **changes))

    def delete(self, name):
        if self.get(name) is None:
            raise KeyError(name)
        self.Entry.from_key(self.conf.key_prefix + name, app=self.app).delete()

    def beat_scheduler_class(self):
        return "celery_manager.beat:RedBeatScheduler"
