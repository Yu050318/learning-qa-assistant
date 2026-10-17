from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, MetaData, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    metadata = MetaData(schema="rag")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('user', 'admin')", name="ck_users_platform_role"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(32), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(12), default="user", server_default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (Index("ix_auth_sessions_user_active", "user_id", "revoked_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("rag.users.id", ondelete="CASCADE"))
    refresh_token_hash: Mapped[str] = mapped_column(String(64))
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DocumentRecord(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("user_id", "content_hash"),
        CheckConstraint("status IN ('processing', 'ready', 'failed', 'deleting')"),
        Index("ix_documents_user_status", "user_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("rag.users.id"))
    original_name: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[str] = mapped_column(String(12))
    file_path: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int | None]
    status: Mapped[str] = mapped_column(String(20), default="processing")
    chunk_count: Mapped[int] = mapped_column(default=0)
    embedding_model: Mapped[str] = mapped_column(String(200))
    error_message: Mapped[str | None] = mapped_column(Text)
    parser_metadata: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        CheckConstraint("state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')"),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1"),
        CheckConstraint("kind IN ('ingest', 'hierarchy')", name="ck_ingestion_jobs_kind"),
        Index("ix_ingestion_jobs_claim", "state", "available_at", "lease_expires_at"),
    )

    document_id: Mapped[UUID] = mapped_column(ForeignKey("rag.documents.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("rag.users.id"))
    kind: Mapped[str] = mapped_column(String(16), default="ingest", server_default="ingest")
    state: Mapped[str] = mapped_column(String(20), default="queued")
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int]
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[UUID | None]
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    configuration_hash: Mapped[str] = mapped_column(String(64))
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100))
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("rag.users.id"))


class OrganizationMember(Base):
    __tablename__ = "organization_members"
    __table_args__ = (CheckConstraint("role IN ('owner', 'admin', 'member')"),)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("rag.organizations.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("rag.users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(12))


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (UniqueConstraint("organization_id", "id"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("rag.organizations.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))


class KnowledgeGrant(Base):
    __tablename__ = "knowledge_grants"
    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "user_id"], ["rag.organization_members.organization_id", "rag.organization_members.user_id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["organization_id", "knowledge_base_id"], ["rag.knowledge_bases.organization_id", "rag.knowledge_bases.id"], ondelete="CASCADE"),
        CheckConstraint("role IN ('viewer', 'editor')"),
    )
    knowledge_base_id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    role: Mapped[str] = mapped_column(String(12))


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"
    knowledge_base_id: Mapped[UUID] = mapped_column(ForeignKey("rag.knowledge_bases.id", ondelete="CASCADE"), primary_key=True)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("rag.documents.id", ondelete="CASCADE"), primary_key=True)


class HierarchyNode(Base):
    __tablename__ = "hierarchy_nodes"
    __table_args__ = (
        UniqueConstraint("node_id", "document_id", "user_id"),
        UniqueConstraint("document_id", "leaf_index"),
        ForeignKeyConstraint(["parent_id", "document_id", "user_id"], ["rag.hierarchy_nodes.node_id", "rag.hierarchy_nodes.document_id", "rag.hierarchy_nodes.user_id"], ondelete="CASCADE"),
        CheckConstraint("(level = 1 AND parent_id IS NULL) OR (level IN (2,3) AND parent_id IS NOT NULL)"),
        CheckConstraint("(level = 3 AND leaf_index IS NOT NULL) OR (level IN (1,2) AND leaf_index IS NULL)"),
        Index("ix_hierarchy_document_parent", "document_id", "parent_id"),
    )
    node_id: Mapped[UUID] = mapped_column(primary_key=True)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("rag.documents.id", ondelete="CASCADE"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("rag.users.id"))
    parent_id: Mapped[UUID | None]
    level: Mapped[int]
    leaf_index: Mapped[int | None]
    text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    node_metadata: Mapped[dict] = mapped_column(JSON().with_variant(JSONB,"postgresql"))


class DocumentSeries(Base):
    __tablename__ = "document_series"
    __table_args__ = (CheckConstraint("next_version >= 2"), Index("ix_document_series_user", "user_id"))

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("rag.users.id", ondelete="CASCADE"))
    current_document_id: Mapped[UUID | None] = mapped_column(ForeignKey("rag.documents.id", ondelete="SET NULL"))
    next_version: Mapped[int] = mapped_column(default=2)


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("series_id", "version_number"), CheckConstraint("version_number >= 1"))

    document_id: Mapped[UUID] = mapped_column(ForeignKey("rag.documents.id", ondelete="CASCADE"), primary_key=True)
    series_id: Mapped[UUID] = mapped_column(ForeignKey("rag.document_series.id"))
    version_number: Mapped[int]


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (
        Index("ix_sessions_user_updated", "user_id", "updated_at"),
        UniqueConstraint("user_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("rag.users.id"))
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text, default="")
    summarized_through: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant', 'system')"),
        ForeignKeyConstraint(["user_id", "session_id"], ["rag.chat_sessions.user_id", "rag.chat_sessions.id"], ondelete="CASCADE"),
        Index("ix_messages_session_created", "session_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID]
    session_id: Mapped[UUID]
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    model_provider: Mapped[str | None] = mapped_column(String(40))
    model_name: Mapped[str | None] = mapped_column(String(200))
    citations: Mapped[list] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=list, server_default="[]")
    token_usage: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    run_metadata: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatRun(Base):
    __tablename__ = "chat_runs"
    __table_args__ = (
        ForeignKeyConstraint(["user_id", "session_id"], ["rag.chat_sessions.user_id", "rag.chat_sessions.id"], ondelete="CASCADE"),
        UniqueConstraint("user_id", "client_key"),
        CheckConstraint("state IN ('running', 'succeeded', 'failed', 'interrupted')"),
        Index("ix_chat_runs_session_active", "session_id", unique=True,
              postgresql_where=text("state = 'running'"), sqlite_where=text("state = 'running'")),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID]
    session_id: Mapped[UUID]
    client_key: Mapped[UUID]
    fingerprint: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20), default="running")
    sequence: Mapped[int] = mapped_column(default=0)
    event_bytes: Mapped[int] = mapped_column(default=0)
    request_id: Mapped[str] = mapped_column(String(100))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    message_id: Mapped[UUID | None] = mapped_column(ForeignKey("rag.chat_messages.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatRunEvent(Base):
    __tablename__ = "chat_run_events"

    run_id: Mapped[UUID] = mapped_column(ForeignKey("rag.chat_runs.id", ondelete="CASCADE"), primary_key=True)
    sequence: Mapped[int] = mapped_column(primary_key=True)
    event: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"))


class ModelConfiguration(Base):
    __tablename__ = "model_configuration"
    __table_args__ = (CheckConstraint("id = 1 AND revision >= 1"),)
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    revision: Mapped[int] = mapped_column(default=1)
    configuration: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    health: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ModelCall(Base):
    __tablename__ = "model_calls"
    __table_args__ = (
        ForeignKeyConstraint(["user_id", "session_id"], ["rag.chat_sessions.user_id", "rag.chat_sessions.id"], ondelete="CASCADE"),
        UniqueConstraint("user_id", "request_id", "operation", "attempt_index"),
        CheckConstraint("status IN ('started', 'succeeded', 'failed')"),
        Index("ix_model_calls_user_created", "user_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID]
    session_id: Mapped[UUID]
    request_id: Mapped[str] = mapped_column(String(100))
    operation: Mapped[str] = mapped_column(String(30))
    attempt_index: Mapped[int]
    configuration_revision: Mapped[int]
    provider: Mapped[str] = mapped_column(String(30))
    model_name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(12), default="started")
    fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    total_tokens: Mapped[int | None]
    elapsed_ms: Mapped[int | None]
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserMemory(Base):
    __tablename__ = "user_memories"
    __table_args__ = (
        UniqueConstraint("user_id", "content_hash"),
        CheckConstraint("status IN ('processing', 'ready', 'failed', 'deleting')"),
        CheckConstraint("created_via IN ('chat', 'management_ui')"),
        Index("ix_memories_user_status", "user_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("rag.users.id", ondelete="CASCADE"))
    content: Mapped[str] = mapped_column(String(500))
    content_hash: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(20), default="processing")
    created_via: Mapped[str] = mapped_column(String(20))
    source_session_id: Mapped[UUID | None]
    source_message_id: Mapped[UUID | None]
    vector_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
