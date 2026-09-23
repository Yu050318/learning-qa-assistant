import logging
from functools import cached_property

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
                if not inspect(connection).has_table(table.name, schema="rag"):
                    raise AppError("DATABASE_NOT_INITIALIZED", "请运行 python -m app.bootstrap --postgres", 503)
                connection.execute(select(table).limit(0))

    def initialize(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(CreateSchema("rag", if_not_exists=True))
            Base.metadata.create_all(connection)

    def close(self) -> None:
        if "engine" in self.__dict__:
            self.engine.dispose()
