"""Consent, local memory projections and durable synchronization receipts."""
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.entity.do.base import Base, utcnow


class MemorySettings(Base):
    __tablename__ = "memory_settings"
    __table_args__ = (CheckConstraint("generation >= 1"),)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("rag.users.id", ondelete="CASCADE"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    generation: Mapped[int] = mapped_column(default=1)
    enabled_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AutomaticMemory(Base):
    __tablename__ = "automatic_memories"
    __table_args__ = (
        UniqueConstraint("user_id", "generation", "uri"),
        CheckConstraint("state IN ('ready','deleting','deleted')"),
        Index("ix_automatic_memory_owner", "user_id", "generation", "state"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("rag.users.id", ondelete="CASCADE"))
    generation: Mapped[int]
    uri: Mapped[str] = mapped_column(String(1024))
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(default=1)
    state: Mapped[str] = mapped_column(String(16), default="ready")
    source_session_id: Mapped[UUID | None]
    source_message_id: Mapped[UUID | None]
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MemorySyncJob(Base):
    __tablename__ = "memory_sync_jobs"
    __table_args__ = (
        UniqueConstraint("user_id", "generation", "dedup_key"),
        CheckConstraint("kind IN ('capture','delete','purge')"),
        CheckConstraint("state IN ('pending','running','waiting','succeeded','failed','uncertain','cancelled')"),
        Index("ix_memory_sync_poll", "state", "available_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("rag.users.id", ondelete="CASCADE"))
    generation: Mapped[int]
    dedup_key: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(12))
    state: Mapped[str] = mapped_column(String(16), default="pending")
    receipt: Mapped[dict] = mapped_column(JSON, default=dict)
    owner_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    attempts: Mapped[int] = mapped_column(default=0)
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
