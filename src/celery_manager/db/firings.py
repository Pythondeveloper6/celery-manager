"""One row per beat firing, written by the stamping scheduler."""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from celery_manager.db.identity import label_text
from celery_manager.db.models import Base


class ScheduleFiring(Base):
    __tablename__ = "schedule_firings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    schedule_name: Mapped[str] = mapped_column(label_text(255), index=True)
    occurrence_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Plain string like llm_calls.task_id: the run row may be purged earlier.
    task_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
