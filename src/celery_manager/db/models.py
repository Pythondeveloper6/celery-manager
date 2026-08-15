from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TaskRun(Base):
    __tablename__ = "task_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    task_name: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)  # queued/started/succeeded/failed/retried/skipped/revoked
    queue: Mapped[str | None] = mapped_column(String(255), nullable=True)
    worker: Mapped[str | None] = mapped_column(String(255), nullable=True)
    args: Mapped[str] = mapped_column(Text, default="[]")
    kwargs: Mapped[str] = mapped_column(Text, default="{}")
    eta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    runtime_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    exception: Mapped[str | None] = mapped_column(Text, nullable=True)
    traceback: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    schedule_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)


class PeriodicSchedule(Base):
    __tablename__ = "periodic_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    task_name: Mapped[str] = mapped_column(String(255))
    schedule_spec: Mapped[str] = mapped_column(String(255))  # human-readable, e.g. "2h" or "crontab(minute=0,hour=7)"
    args: Mapped[str] = mapped_column(Text, default="[]")
    kwargs: Mapped[str] = mapped_column(Text, default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class ScheduleOverride(Base):
    __tablename__ = "schedule_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    schedule_name: Mapped[str] = mapped_column(String(255), ForeignKey("periodic_schedules.name"), index=True)
    target_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    action: Mapped[str] = mapped_column(String(32))  # skip | cancel | reschedule
    reschedule_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str] = mapped_column(String(255))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
