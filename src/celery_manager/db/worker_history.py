"""Minute-bucket heartbeat samples, separate from last-seen worker state."""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from celery_manager.db.models import Base


class WorkerHeartbeatSample(Base):
    __tablename__ = "worker_heartbeat_samples"
    hostname: Mapped[str] = mapped_column(String(255), primary_key=True)
    bucket_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    state: Mapped[str] = mapped_column(String(16))
    active: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processed: Mapped[int | None] = mapped_column(Integer, nullable=True)
