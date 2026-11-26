"""Scoped API tokens for scripts (sent as `Authorization: Bearer cmk_...`)."""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from celery_manager.db.identity import label_text
from celery_manager.db.models import Base


class ApiToken(Base):
    __tablename__ = "api_tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(label_text(64), unique=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 hex of the full token
    role: Mapped[str] = mapped_column(String(16))
    owner: Mapped[str] = mapped_column(label_text(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
