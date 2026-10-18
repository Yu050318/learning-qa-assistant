import logging
import hashlib
from contextlib import contextmanager
from functools import cached_property

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema

from app.core.config import Settings
from app.core.errors import AppError
from app.infrastructure.persistence.models import Base, DocumentSeries, DocumentVersion, IngestionJob, ChatRun, ChatRunEvent
from app.infrastructure.persistence.models import Organization, OrganizationMember, KnowledgeBase, KnowledgeGrant, KnowledgeDocument
from app.infrastructure.persistence.models import ModelConfiguration, ModelCall
from app.infrastructure.persistence.models import HierarchyNode


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings

    @cached_property
    def engine(self):
        raw_url = self.settings.database_url.get_secret_value()
        if not raw_url:
            raise AppError("CONFIGURATION_ERROR", "请配置 DB_URL 或 DATABASE_URL", 503)
        try:
            url = make_url(raw_url)
        except Exception:
            raise AppError("CONFIGURATION_ERROR", "数据库连接配置格式无效", 503) from None
        if url.get_backend_name() not in {"postgresql", "postgres"}:
            raise AppError("CONFIGURATION_ERROR", "本服务要求 PostgreSQL 数据库", 503)
        url = url.set(drivername="postgresql+psycopg")
        sslmode = url.query.get("sslmode", "require")
        if self.settings.database_require_tls and sslmode not in {"require", "verify-ca", "verify-full"}:
            raise AppError("DATABASE_TLS_REQUIRED", "数据库需启用 TLS；仅受信任开发环境可设置 DATABASE_REQUIRE_TLS=false", 503)
        if sslmode not in {"require", "verify-ca", "verify-full"}:
            logging.getLogger("rag").warning("database_tls_disabled")
        url = url.update_query_dict({"sslmode": sslmode})
        return create_engine(
            url, pool_pre_ping=True, pool_size=5, max_overflow=5,
            pool_timeout=self.settings.database_timeout, hide_parameters=True,
            connect_args={
                "connect_timeout": self.settings.database_timeout,
                "options": f"-c statement_timeout={self.settings.database_timeout * 1000} -c lock_timeout=3000",
            },
        )

    @cached_property
    def sessions(self):
        return sessionmaker(bind=self.engine, expire_on_commit=False)

    def check(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            for table in Base.metadata.sorted_tables:
                if table.name == "worker_heartbeats" and self.settings.ingestion_mode != "durable" and not self.settings.chat_runs_enabled:
                    continue
                if table.name == "hierarchy_nodes" and not self.settings.hierarchy_enabled:
                    continue
                if table.name in {"model_configuration", "model_configuration_versions", "model_calls"} and not self.settings.model_center_enabled:
                    continue
                if table.name in {"organizations", "organization_members", "knowledge_bases", "knowledge_grants", "knowledge_documents"} and not self.settings.sharing_enabled:
                    continue
                if table.name in {"chat_runs", "chat_run_events", "chat_run_checkpoints", "chat_run_operations", "chat_event_outbox"} and not self.settings.chat_runs_enabled:
                    continue
                if table.name == "ingestion_jobs" and self.settings.ingestion_mode != "durable":
                    continue
                if not self.settings.document_versions_enabled and table.name in {"document_series", "document_versions"}:
                    continue
                if not inspect(connection).has_table(table.name, schema="rag"):
                    raise AppError("DATABASE_NOT_INITIALIZED", "请运行 python -m app.bootstrap --postgres", 503)
                connection.execute(select(table).limit(0))

    def initialize(self) -> None:
        from app.migrate import upgrade
        upgrade(self)

    def initialize_document_versions(self) -> None:
        with self.engine.begin() as connection:
            if not inspect(connection).has_table("documents", schema="rag"):
                raise AppError("DATABASE_NOT_INITIALIZED", "请先显式初始化平台 PostgreSQL schema", 503)
            Base.metadata.create_all(connection, tables=[DocumentSeries.__table__, DocumentVersion.__table__])

    def close(self) -> None:
        if "engine" in self.__dict__:
            self.engine.dispose()

    def initialize_ingestion_jobs(self) -> None:
        with self.engine.begin() as connection:
            if not inspect(connection).has_table("documents", schema="rag"):
                raise AppError("DATABASE_NOT_INITIALIZED", "请先显式初始化平台 PostgreSQL schema", 503)
            Base.metadata.create_all(connection, tables=[IngestionJob.__table__])

    @contextmanager
    def document_lock(self, document_id):
        with self.resource_lock("ingestion", document_id) as probe:
            yield probe

    def initialize_chat_runs(self):
        with self.engine.begin() as connection:
            if not inspect(connection).has_table("chat_messages", schema="rag"):
                raise AppError("DATABASE_NOT_INITIALIZED", "请先显式初始化平台 PostgreSQL schema", 503)
            Base.metadata.create_all(connection, tables=[ChatRun.__table__, ChatRunEvent.__table__])

    def initialize_sharing(self):
        with self.engine.begin() as connection:
            if not inspect(connection).has_table("documents", schema="rag"):
                raise AppError("DATABASE_NOT_INITIALIZED", "请先初始化平台 PostgreSQL schema", 503)
            Base.metadata.create_all(connection, tables=[Organization.__table__, OrganizationMember.__table__, KnowledgeBase.__table__, KnowledgeGrant.__table__, KnowledgeDocument.__table__])

    def initialize_model_center(self):
        with self.engine.connect() as connection:
            if not inspect(connection).has_table("model_configuration_versions", schema="rag"):
                raise AppError("MODEL_CENTER_MIGRATION_REQUIRED", "请先执行 python -m app.migrate，再初始化模型中心", 503)
            connection.execute(select(ModelConfiguration).limit(0))
            connection.execute(select(ModelCall).limit(0))

    def initialize_hierarchy(self):
        with self.engine.begin() as connection:
            if not inspect(connection).has_table("documents", schema="rag"):
                raise AppError("DATABASE_NOT_INITIALIZED", "请先初始化平台 PostgreSQL schema", 503)
            Base.metadata.create_all(connection, tables=[HierarchyNode.__table__])

    @contextmanager
    def resource_lock(self, namespace, resource_id):
        # Session lock spans parser/HTTP/vector calls without holding a SQL transaction.
        key = int.from_bytes(hashlib.sha256(f"{namespace}:{resource_id}".encode()).digest()[:8], "big", signed=True)
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            acquired = False
            try:
                acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
                if not acquired:
                    raise AppError("RESOURCE_BUSY", "文档正在处理中，请稍后重试", 409)
                backend = connection.scalar(text("SELECT pg_backend_pid()"))

                def probe():
                    if connection.scalar(text("SELECT pg_backend_pid()")) != backend:
                        raise AppError("INGESTION_LEASE_LOST", "文档锁连接已改变", 409)

                yield probe
            finally:
                if acquired:
                    try:
                        connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                    except Exception:
                        connection.invalidate()
