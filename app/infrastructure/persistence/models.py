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
        Index("uq_documents_source_build", "user_id", "content_hash", "build_fingerprint", unique=True,
              postgresql_where=text("status != 'retired' AND organization_id IS NULL"), sqlite_where=text("status != 'retired' AND organization_id IS NULL")),
        Index("uq_documents_workspace_source_build", "user_id", "organization_id", "content_hash", "build_fingerprint", unique=True,
              postgresql_where=text("status != 'retired' AND organization_id IS NOT NULL"), sqlite_where=text("status != 'retired' AND organization_id IS NOT NULL")),
        CheckConstraint("status IN ('processing', 'ready', 'failed', 'deleting', 'retired')", name="ck_documents_status"),
        Index("ix_documents_user_status", "user_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("rag.users.id"))
    original_name: Mapped[str] = mapped_column(String(255))
    organization_id: Mapped[UUID | None] = mapped_column(ForeignKey("rag.organizations.id"))
    file_type: Mapped[str] = mapped_column(String(12))
    file_path: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    build_fingerprint: Mapped[str] = mapped_column(String(64), default="legacy", server_default="legacy")
    size_bytes: Mapped[int | None]
    status: Mapped[str] = mapped_column(String(20), default="processing")
    chunk_count: Mapped[int] = mapped_column(default=0)
    embedding_model: Mapped[str] = mapped_column(String(200))
    error_message: Mapped[str | None] = mapped_column(Text)
    parser_metadata: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class DocumentIndex(Base):
    __tablename__ = "document_indexes"
    __table_args__ = (
        CheckConstraint("state IN ('building','verified','published','superseded','retiring','cleaned','failed')", name="ck_document_indexes_state"),
        CheckConstraint("leaf_count >= 0 AND embedding_dimension >= 64", name="ck_document_indexes_bounds"),
    )
    document_id: Mapped[UUID] = mapped_column(ForeignKey("rag.documents.id", ondelete="CASCADE"), primary_key=True)
    collection_name: Mapped[str] = mapped_column(String(160))
    database_name: Mapped[str] = mapped_column(String(128))
    embedding_model: Mapped[str] = mapped_column(String(200))
    embedding_dimension: Mapped[int]
    connection_sha256: Mapped[str] = mapped_column(String(64))
    build_fingerprint: Mapped[str] = mapped_column(String(64))
    source_sha256: Mapped[str] = mapped_column(String(64))
    leaf_sha256: Mapped[str | None] = mapped_column(String(64))
    leaf_count: Mapped[int] = mapped_column(default=0, server_default="0")
    state: Mapped[str] = mapped_column(String(16), default="building", server_default="building")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class DocumentTask(Base):
    __tablename__ = "document_tasks"
    __table_args__ = (
        CheckConstraint("kind IN ('ingest','hierarchy','delete','delete_series')", name="ck_document_tasks_kind"),
        CheckConstraint("state IN ('queued','running','succeeded','failed','cancelled')", name="ck_document_tasks_state"),
        Index("ix_document_tasks_owner_created", "user_id", "created_at"),
        Index("ix_document_tasks_parent", "parent_task_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    document_id: Mapped[UUID | None] = mapped_column(ForeignKey("rag.documents.id", ondelete="SET NULL"))
    parent_task_id: Mapped[UUID | None] = mapped_column(ForeignKey("rag.document_tasks.id"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("rag.users.id"))
    original_name: Mapped[str] = mapped_column(String(255))
    organization_id: Mapped[UUID | None] = mapped_column(ForeignKey("rag.organizations.id"))
    kind: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16), default="queued")
    phase: Mapped[str] = mapped_column(String(40), default="queued")
    processed_units: Mapped[int] = mapped_column(default=0)
    total_units: Mapped[int | None]
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int]
    error_code: Mapped[str | None] = mapped_column(String(80))
    details: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        CheckConstraint("state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')"),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1"),
        CheckConstraint("kind IN ('ingest', 'hierarchy', 'delete')", name="ck_ingestion_jobs_kind"),
        Index("ix_ingestion_jobs_claim", "state", "available_at", "lease_expires_at"),
    )

    document_id: Mapped[UUID] = mapped_column(ForeignKey("rag.documents.id", ondelete="CASCADE"), primary_key=True)
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("rag.document_tasks.id"))
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
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    __table_args__ = (Index("uq_knowledge_documents_series_current", "knowledge_base_id", "series_id", unique=True,
                           postgresql_where=text("is_current"), sqlite_where=text("is_current")),)
    knowledge_base_id: Mapped[UUID] = mapped_column(ForeignKey("rag.knowledge_bases.id", ondelete="CASCADE"), primary_key=True)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("rag.documents.id", ondelete="CASCADE"), primary_key=True)
    series_id: Mapped[UUID]
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


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
    retirement_task_id: Mapped[UUID | None] = mapped_column(ForeignKey("rag.document_tasks.id"))


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
        CheckConstraint("kind IN ('chat','evaluation')", name="ck_chat_sessions_kind"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("rag.users.id"))
    organization_id: Mapped[UUID | None] = mapped_column(ForeignKey("rag.organizations.id"))
    title: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(12), default="chat", server_default="chat")
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
        Index("ix_chat_runs_claim", "state", "lease_expires_at", "created_at"),
        CheckConstraint("state IN ('queued', 'running', 'waiting', 'cancelling', 'cancelled', 'succeeded', 'failed', 'interrupted')", name="ck_chat_runs_state"),
        CheckConstraint("execution_kind IN ('chat','evaluation')", name="ck_chat_runs_kind"),
        Index("ix_chat_runs_session_active", "session_id", unique=True,
              postgresql_where=text("state IN ('queued', 'running', 'waiting', 'cancelling')"),
              sqlite_where=text("state IN ('queued', 'running', 'waiting', 'cancelling')")),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID]
    session_id: Mapped[UUID]
    client_key: Mapped[UUID]
    fingerprint: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20), default="running")
    execution_kind: Mapped[str] = mapped_column(String(12), default="chat", server_default="chat")
    result_snapshot: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=dict, server_default="{}")
    sequence: Mapped[int] = mapped_column(default=0)
    event_bytes: Mapped[int] = mapped_column(default=0)
    request_id: Mapped[str] = mapped_column(String(100))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    message_id: Mapped[UUID | None] = mapped_column(ForeignKey("rag.chat_messages.id", ondelete="SET NULL"))
    user_message_id: Mapped[UUID | None] = mapped_column(ForeignKey("rag.chat_messages.id", ondelete="SET NULL"))
    payload: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=dict, server_default="{}")
    execution_snapshot: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=dict, server_default="{}")
    owner_token: Mapped[UUID | None]
    generation: Mapped[int] = mapped_column(default=0, server_default="0")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    remaining_seconds: Mapped[float | None]
    active_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resume_key: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RunIndexPin(Base):
    __tablename__ = "run_index_pins"
    __table_args__ = (Index("ix_run_index_pins_document", "document_id"),)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("rag.chat_runs.id", ondelete="CASCADE"), primary_key=True)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("rag.documents.id"), primary_key=True)


class ChatRunEvent(Base):
    __tablename__ = "chat_run_events"

    run_id: Mapped[UUID] = mapped_column(ForeignKey("rag.chat_runs.id", ondelete="CASCADE"), primary_key=True)
    sequence: Mapped[int] = mapped_column(primary_key=True)
    event: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"))


class ChatRunCheckpoint(Base):
    __tablename__ = "chat_run_checkpoints"
    __table_args__ = (CheckConstraint("revision >= 1"),)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("rag.chat_runs.id", ondelete="CASCADE"), primary_key=True)
    revision: Mapped[int] = mapped_column(default=1)
    payload: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ChatRunOperation(Base):
    __tablename__ = "chat_run_operations"
    __table_args__ = (CheckConstraint("state IN ('started', 'succeeded', 'uncertain')"),)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("rag.chat_runs.id", ondelete="CASCADE"), primary_key=True)
    operation_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    input_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), default="started")
    readonly: Mapped[bool] = mapped_column(Boolean)
    owner_generation: Mapped[int]
    attempts: Mapped[int] = mapped_column(default=1)
    result: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ChatEventOutbox(Base):
    __tablename__ = "chat_event_outbox"
    __table_args__ = (
        ForeignKeyConstraint(["run_id", "sequence"], ["rag.chat_run_events.run_id", "rag.chat_run_events.sequence"], ondelete="CASCADE"),
        Index("ix_chat_event_outbox_pending", "run_id", "sequence", postgresql_where=text("dispatched_at IS NULL"), sqlite_where=text("dispatched_at IS NULL")),
    )
    run_id: Mapped[UUID] = mapped_column(primary_key=True)
    sequence: Mapped[int] = mapped_column(primary_key=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModelConfiguration(Base):
    __tablename__ = "model_configuration"
    __table_args__ = (CheckConstraint("id = 1 AND revision >= 1"),)
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    revision: Mapped[int] = mapped_column(default=1)
    configuration: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    health: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ModelConfigurationVersion(Base):
    __tablename__ = "model_configuration_versions"
    __table_args__ = (CheckConstraint("revision >= 1", name="ck_model_configuration_versions_revision"),)
    revision: Mapped[int] = mapped_column(primary_key=True)
    configuration: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    created_by: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelCall(Base):
    __tablename__ = "model_calls"
    __table_args__ = (
        ForeignKeyConstraint(["user_id", "session_id"], ["rag.chat_sessions.user_id", "rag.chat_sessions.id"], ondelete="CASCADE"),
        UniqueConstraint("user_id", "request_id", "operation", "attempt_index"),
        CheckConstraint("status IN ('started', 'succeeded', 'failed')"),
        CheckConstraint("(model_role IS NULL AND model_profile_id IS NULL AND model_profile_version IS NULL) OR "
                        "(model_role IS NOT NULL AND model_role IN ('answer','fast','grader','evaluator') AND "
                        "model_profile_id IS NOT NULL AND model_profile_version IS NOT NULL AND model_profile_version >= 1)",
                        name="ck_model_calls_profile"),
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
    model_role: Mapped[str | None] = mapped_column(String(20))
    model_profile_id: Mapped[str | None] = mapped_column(String(64))
    model_profile_version: Mapped[int | None]
    status: Mapped[str] = mapped_column(String(12), default="started")
    fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    total_tokens: Mapped[int | None]
    elapsed_ms: Mapped[int | None]
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvaluationDataset(Base):
    __tablename__ = "evaluation_datasets"
    __table_args__ = (UniqueConstraint("created_by", "fingerprint"), CheckConstraint("case_count >= 1 AND case_count <= 1000"))
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("rag.users.id"))
    name: Mapped[str] = mapped_column(String(120))
    fingerprint: Mapped[str] = mapped_column(String(64))
    case_count: Mapped[int]
    content: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvaluationJob(Base):
    __tablename__ = "evaluation_jobs"
    __table_args__ = (
        UniqueConstraint("created_by", "client_key"),
        CheckConstraint("state IN ('queued','running','cancelling','cancelled','succeeded','failed')", name="ck_evaluation_jobs_state"),
        CheckConstraint("completed_cases >= 0 AND completed_cases <= total_cases AND total_cases >= 1", name="ck_evaluation_jobs_progress"),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1 AND generation >= 0", name="ck_evaluation_jobs_attempts"),
        Index("ix_evaluation_jobs_claim", "state", "available_at", "lease_expires_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("rag.users.id"))
    dataset_id: Mapped[UUID] = mapped_column(ForeignKey("rag.evaluation_datasets.id"))
    baseline_job_id: Mapped[UUID | None] = mapped_column(ForeignKey("rag.evaluation_jobs.id"))
    client_key: Mapped[UUID]
    fingerprint: Mapped[str] = mapped_column(String(64))
    dataset_fingerprint: Mapped[str] = mapped_column(String(64))
    gate_policy: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=dict)
    split: Mapped[str] = mapped_column(String(12))
    profile: Mapped[str] = mapped_column(String(12))
    model_snapshot: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    scope_snapshot: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    state: Mapped[str] = mapped_column(String(12), default="queued")
    completed_cases: Mapped[int] = mapped_column(default=0)
    total_cases: Mapped[int]
    owner_token: Mapped[UUID | None]
    generation: Mapped[int] = mapped_column(default=0)
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    report: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvaluationCaseResult(Base):
    __tablename__ = "evaluation_cases"
    __table_args__ = (
        UniqueConstraint("job_id", "case_id"), UniqueConstraint("job_id", "position"),
        CheckConstraint("state IN ('queued','running','succeeded','failed','cancelled')", name="ck_evaluation_cases_state"),
        CheckConstraint("position >= 0", name="ck_evaluation_cases_position"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("rag.evaluation_jobs.id", ondelete="CASCADE"))
    case_id: Mapped[str] = mapped_column(String(100))
    position: Mapped[int]
    state: Mapped[str] = mapped_column(String(12), default="queued")
    label: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    session_id: Mapped[UUID | None] = mapped_column(ForeignKey("rag.chat_sessions.id"))
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("rag.chat_runs.id"), unique=True)
    prediction: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    review: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    judge_receipt: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    result: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    error_code: Mapped[str | None] = mapped_column(String(100))
    duration_ms: Mapped[int | None]
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
