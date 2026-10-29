"""Optional stamping SQL scheduler with an engine isolated per instance."""
from multiprocessing.util import Finalize, register_after_fork

from celery.beat import Scheduler as CeleryScheduler
from sqlalchemy.pool import StaticPool
from sqlalchemy_celery_beat.schedulers import DEFAULT_MAX_INTERVAL, DatabaseScheduler

from celery_manager.beat import StampOccurrencesMixin
from celery_manager.schedulers.sqlalchemy import SQLAlchemyBeatBackend


class SQLAlchemyDatabaseScheduler(StampOccurrencesMixin, DatabaseScheduler):
    def __init__(self, *args, **kwargs):
        app = kwargs.get("app", args[0] if args else None)
        if app is None:
            raise ValueError("SQLAlchemyDatabaseScheduler requires a Celery app")
        self.app = app
        self.dburi = kwargs.pop("dburi", None) or app.conf.get("beat_dburi")
        self.schema = kwargs.pop("schema", None) or app.conf.get("beat_schema")
        self.engine_options = kwargs.pop("engine_options", None) or app.conf.get("beat_engine_options") or {}
        backend = SQLAlchemyBeatBackend(app, self.dburi, schema=self.schema, engine_options=self.engine_options)
        self.engine, self.Session = backend.engine, backend.sessions
        self._dirty = set()
        # Upstream's global SessionManager caches by URL, not schema. Build the
        # per-instance storage above, retaining upstream tick/reserve/sync logic.
        CeleryScheduler.__init__(self, *args, **kwargs)
        self._finalize = Finalize(self, self.sync, exitpriority=5)
        self.max_interval = kwargs.get("max_interval") or app.conf.beat_max_loop_interval or DEFAULT_MAX_INTERVAL
        register_after_fork(self, type(self)._after_fork)

    def setup_schedule(self):
        # SQL owns definitions. Importing app.conf.beat_schedule on startup
        # would silently undo operator edits; adoption is an explicit plan.
        self.install_default_entries(self.schedule)

    def _after_fork(self):
        if not isinstance(self.engine.pool, StaticPool):
            self.engine.dispose(close=False)

    def close(self):
        try:
            return super().close()
        finally:
            self.engine.dispose()
