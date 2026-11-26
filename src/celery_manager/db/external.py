"""Outbound calls made inside tracked runs (celery_manager.contrib.http)."""
from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from celery_manager.db.identity import label_text
from celery_manager.db.models import Base


class ExternalCall(Base):
    __tablename__ = "external_calls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(255), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(16))  # "http"
    # scheme://host[:port] only: never the path, query string, headers or body.
    target: Mapped[str] = mapped_column(label_text(255), index=True)
    method: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(String(128), nullable=True)  # exception class only
    bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
