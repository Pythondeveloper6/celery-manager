"""Durable revokes and per-attempt history of task runs."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from celery_manager.db.identity import label_text, payload_text
from celery_manager.db.models import Base


class RevokedTask(Base):
    """A revoke to re-send to workers that come online before it expires,
    and to honour at run start (workers forget revokes on restart)."""
    __tablename__ = "revoked_tasks"
    task_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    terminate: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    requested_by: Mapped[str | None] = mapped_column(label_text(255), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class TaskRunAttempt(Base):
    """One attempt of a run; Celery reuses the task id across retries."""
    __tablename__ = "task_run_attempts"
    __table_args__ = (UniqueConstraint("task_id", "attempt", name="uq_task_run_attempts_task_id_attempt"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(255), index=True)
    attempt: Mapped[int] = mapped_column(Integer)  # 0 for the first, = request.retries
    status: Mapped[str] = mapped_column(String(32))
    worker: Mapped[str | None] = mapped_column(label_text(255), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    runtime_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    exception: Mapped[str | None] = mapped_column(payload_text(), nullable=True)
    traceback: Mapped[str | None] = mapped_column(payload_text(), nullable=True)
