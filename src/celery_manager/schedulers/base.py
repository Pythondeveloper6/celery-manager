from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from celery.schedules import maybe_schedule


@dataclass(frozen=True)
class ScheduleEntry:
    name: str
    task: str
    schedule: object
    args: list = field(default_factory=list)
    kwargs: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)
    enabled: bool = True
    last_run_at: datetime | None = None
    source: str = ""

    def as_beat_entry(self):
        return {"task": self.task, "schedule": self.schedule, "args": self.args,
                "kwargs": self.kwargs, "options": self.options, "enabled": self.enabled,
                "last_run_at": self.last_run_at}


class SchedulerBackend(Protocol):
    name: str
    writable: bool
    def list(self) -> Iterable[ScheduleEntry]: ...
    def get(self, name: str) -> ScheduleEntry | None: ...
    def create(self, entry: ScheduleEntry) -> ScheduleEntry: ...
    def update(self, name: str, **changes) -> ScheduleEntry: ...
    def delete(self, name: str) -> None: ...
    def beat_scheduler_class(self) -> str: ...


class StaticBeatBackend:
    name = "static"
    writable = False

    def __init__(self, celery_app):
        self.app = celery_app

    def list(self):
        return [ScheduleEntry(name, e["task"], maybe_schedule(e["schedule"], app=self.app),
                              list(e.get("args") or []), dict(e.get("kwargs") or {}),
                              dict(e.get("options") or {}), source=self.name)
                for name, e in (self.app.conf.beat_schedule or {}).items()]

    def get(self, name):
        return next((e for e in self.list() if e.name == name), None)

    def create(self, entry):
        raise PermissionError("Static beat schedules are read-only")

    def update(self, name, **changes):
        raise PermissionError("Static beat schedules are read-only")

    def delete(self, name):
        raise PermissionError("Static beat schedules are read-only")

    def beat_scheduler_class(self):
        return "celery_manager.beat:Scheduler"
