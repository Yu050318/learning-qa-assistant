import logging
from functools import cached_property
from pathlib import Path

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema

from app.core.config import Settings
from app.core.errors import AppError
from app.infrastructure.persistence.models import Base


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
                if not inspect(connection).has_table(table.name, schema="rag_v1"):
                    raise AppError("DATABASE_NOT_INITIALIZED", "请运行 python -m app.bootstrap --postgres", 503)
                connection.execute(select(table).limit(0))
            if not inspect(connection).has_table("schema_migrations", schema="rag_v1") or not connection.scalar(
                text("SELECT count(*) = 2 FROM rag_v1.schema_migrations WHERE version IN ('001_agent_v2', '002_frontend_v3')")
            ):
                raise AppError("DATABASE_MIGRATION_REQUIRED", "请运行 python -m app.bootstrap --migrate-v2", 503)

    def initialize(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(CreateSchema("rag_v1", if_not_exists=True))
            Base.metadata.create_all(connection)

    def migrate_v2(self) -> None:
        with self.engine.connect() as connection:
            for name in ("001_agent_v2.sql", "002_frontend_v3.sql"):
                script = Path(__file__).resolve().parents[3] / "migrations" / name
                connection.exec_driver_sql(script.read_text(encoding="utf-8"))

    def close(self) -> None:
        if "engine" in self.__dict__:
            self.engine.dispose()
