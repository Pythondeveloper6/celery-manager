"""Host-neutral probe observations and expected scheduler registries."""
from datetime import datetime

from sqlalchemy import DateTime, Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from celery_manager.db.models import Base


class HealthObservation(Base):
    __tablename__ = "health_observations"
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(16))
    metrics: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class SchedulerInstance(Base):
    __tablename__ = "scheduler_instances"
    instance_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExpectedSchedulerCommand(Base):
    __tablename__ = "expected_scheduler_commands"
    instance_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    schedule_spec: Mapped[str] = mapped_column(Text)
    grace_seconds: Mapped[float] = mapped_column(Float)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
