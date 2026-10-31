"""Operational timeline annotations, independent of host application tables."""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UnicodeText
from sqlalchemy.dialects.mysql import TEXT
from sqlalchemy.orm import Mapped, mapped_column

from celery_manager.db.identity import identity_type
from celery_manager.db.models import Base


class OperationAnnotation(Base):
    __tablename__ = "operation_annotations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))
    environment: Mapped[str] = mapped_column(identity_type(100), index=True)
    label: Mapped[str] = mapped_column(identity_type())
    revision: Mapped[str | None] = mapped_column(identity_type(), nullable=True)
    note: Mapped[str | None] = mapped_column(UnicodeText().with_variant(TEXT(charset="utf8mb4"), "mysql"), nullable=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor: Mapped[str] = mapped_column(String(255))
    actor_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
