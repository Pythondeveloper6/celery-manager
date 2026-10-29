"""Durable schedules owned by sqlalchemy-celery-beat's own SQL tables."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from .base import ScheduleEntry


class SQLAlchemyBeatBackend:
    name = "sqlalchemy-beat"
    writable = True

    def __init__(self, celery_app, db_url=None, *, schema=None, engine_options=None, create_tables=False):
        # Importing celery_manager or its scheduler registry never imports this
        # dependency. Each backend has its own engine and namespace mapping.
        from sqlalchemy_celery_beat.models import PeriodicTask
        from sqlalchemy_celery_beat.session import ModelBase

        from celery_manager.db.session import create_db_engine
        self.app = celery_app
        db_url = db_url or celery_app.conf.get("beat_dburi")
        if not db_url:
            raise ValueError("Set beat_dburi or pass db_url to SQLAlchemyBeatBackend")
        self.db_url = db_url
        self.schema = schema if schema is not None else celery_app.conf.get("beat_schema")
        self.engine = create_db_engine(db_url, engine_options=engine_options).execution_options(
            schema_translate_map={"celery_schema": self.schema})
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.Model, self.metadata = PeriodicTask, ModelBase.metadata
        if create_tables:
            self.initialize()

    def initialize(self):
        """Explicit deployment step. Provision named schemas before calling."""
        self.metadata.create_all(self.engine)

    def close(self):
        self.engine.dispose()

    def _convert(self, model):
        if model.one_off or model.start_time is not None:
            raise ValueError(f"Schedule {model.name!r} uses unsupported one_off/start_time semantics")
        options = {key: getattr(model, key) for key in ("queue", "exchange", "routing_key", "priority")
                   if getattr(model, key) is not None}
        headers = json.loads(model.headers or "{}")
        if headers:
            options["headers"] = headers
        if model.expires is not None:
            options["expires"] = (model.expires.replace(tzinfo=timezone.utc)
                                  if model.expires.tzinfo is None else model.expires.astimezone(timezone.utc))
        elif model.expire_seconds is not None:
            options["expires"] = model.expire_seconds
        stamp = model.last_run_at
        if stamp is not None and stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        timing = model.schedule
        from celery.schedules import crontab
        if isinstance(timing, crontab):
            from celery_manager.schedule_specs import spec_from_schedule, validate_spec
            spec = spec_from_schedule(timing)
            spec["timezone"] = str(timing.tz)
            _, timing = validate_spec(spec, app=self.app)
        return ScheduleEntry(model.name, model.task, timing, json.loads(model.args),
                             json.loads(model.kwargs), options, model.enabled, stamp, self.name)

    def list(self):
        with self.sessions() as session:
            return [self._convert(row) for row in session.scalars(select(self.Model).order_by(self.Model.name))]

    def get(self, name):
        with self.sessions() as session:
            row = session.scalar(select(self.Model).where(self.Model.name == name))
            return self._convert(row) if row is not None else None

    def _schedule_model(self, session, schedule):
        from celery.schedules import crontab, solar
        from celery.schedules import schedule as interval
        from sqlalchemy_celery_beat.models import (
            CrontabSchedule,
            IntervalSchedule,
            Period,
            SolarSchedule,
        )

        from celery_manager.schedule_specs import spec_from_schedule
        if isinstance(schedule, crontab):
            spec = spec_from_schedule(schedule)
            fields = {key: spec[key] for key in ("minute", "hour", "day_of_week", "day_of_month", "month_of_year")}
            tz = schedule.tz
            fields["timezone"] = getattr(tz, "key", None) or getattr(tz, "zone", None) or str(tz)
            model = CrontabSchedule
        elif isinstance(schedule, solar):
            model, fields = SolarSchedule, {"event": schedule.event, "latitude": schedule.lat, "longitude": schedule.lon}
        elif isinstance(schedule, interval):
            if schedule.relative:
                raise ValueError("Relative interval rounding cannot be preserved by the SQL scheduler")
            seconds = schedule.run_every.total_seconds()
            if not seconds.is_integer() or not 1 <= seconds <= 2_147_483_647:
                raise ValueError("SQL schedule intervals must be whole seconds between 1 and 2147483647")
            model, fields = IntervalSchedule, {"every": int(seconds), "period": Period.SECONDS}
        else:
            raise TypeError(f"Unsupported SQL schedule type: {type(schedule).__name__}")
        row = session.scalar(select(model).filter_by(**fields).limit(1))
        if row is None:
            row = model(**fields)
            session.add(row)
            session.flush()
        return row

    def _fields(self, session, entry):
        allowed = {"queue", "exchange", "routing_key", "priority", "headers", "expires"}
        if set(entry.options) - allowed:
            raise ValueError("Unsupported SQL scheduler delivery option")
        fields = {key: entry.options.get(key) for key in ("queue", "exchange", "routing_key", "priority")}
        fields.update(task=entry.task, args=json.dumps(entry.args), kwargs=json.dumps(entry.kwargs),
                      headers=json.dumps(entry.options.get("headers") or {}), enabled=entry.enabled,
                      schedule_model=self._schedule_model(session, entry.schedule), expires=None, expire_seconds=None)
        expires = entry.options.get("expires")
        if isinstance(expires, str):
            expires = datetime.fromisoformat(expires)
        if isinstance(expires, datetime):
            if expires.tzinfo is None:
                raise ValueError("Absolute expiry must include a timezone")
            fields["expires"] = expires.astimezone(timezone.utc)
        elif expires is not None:
            if isinstance(expires, bool) or not isinstance(expires, int) or expires < 0:
                raise ValueError("Expiry must be an ISO timestamp or nonnegative whole seconds")
            fields["expire_seconds"] = expires
        return fields

    def create(self, entry):
        try:
            with self.sessions.begin() as session:
                if session.scalar(select(self.Model.id).where(self.Model.name == entry.name)) is not None:
                    raise FileExistsError(entry.name)
                row = self.Model(name=entry.name, **self._fields(session, entry))
                session.add(row)
                session.flush()
                result = self._convert(row)
            return result
        except IntegrityError as exc:
            if self.get(entry.name) is not None:
                raise FileExistsError(entry.name) from exc
            raise

    def update(self, name, **changes):
        with self.sessions.begin() as session:
            row = session.scalar(select(self.Model).where(self.Model.name == name).with_for_update())
            if row is None:
                raise KeyError(name)
            entry = replace(self._convert(row), **changes)
            for key, value in self._fields(session, entry).items():
                setattr(row, key, value)
            session.flush()
            return self._convert(row)

    def delete(self, name):
        with self.sessions.begin() as session:
            row = session.scalar(select(self.Model).where(self.Model.name == name).with_for_update())
            if row is None:
                raise KeyError(name)
            session.delete(row)

    def beat_scheduler_class(self):
        return "celery_manager.beat:SQLAlchemyDatabaseScheduler"
