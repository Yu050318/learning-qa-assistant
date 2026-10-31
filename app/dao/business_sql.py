"""Read-only PostgreSQL execution; the manager validates incoming SQL."""
from app.dao.queries import sql as queries
from app.dao.postgres.errors import SQLAlchemyError
from app.core.errors import AppError
import json
import re
from time import monotonic
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")

class ReadOnlySQL:
    def __init__(self, settings):
        self.settings = settings
        self.engine = None

    @property
    def configured(self) -> bool:
        return bool(self.settings.sql_database_url.get_secret_value() and self.settings.sql_allowed_tables)

    def _engine(self):
        if not self.configured:
            raise AppError("SQL_UNAVAILABLE", "业务只读数据库尚未配置", 503)
        allowed = self.settings.sql_allowed_tables
        if any(not _IDENTIFIER.fullmatch(table) or table.split(".")[0] in {
            "rag", "pg_catalog", "information_schema", "pg_toast",
        } or table.split(".")[0].startswith("pg_") for table in allowed):
            raise AppError("SQL_CONFIGURATION_ERROR", "业务表白名单配置无效", 503)
        if self.engine is None:
            try:
                url = queries.business_url(self)
                platform = queries.platform_url(self)
                if url.drivername not in {"postgresql", "postgresql+psycopg"}:
                    raise ValueError
                if platform and (url.host, url.port or 5432, url.database) == (platform.host, platform.port or 5432, platform.database):
                    raise ValueError
                self.engine = queries.create_readonly_engine(self, url)
            except (ValueError, SQLAlchemyError):
                raise AppError("SQL_CONFIGURATION_ERROR", "请配置独立的 PostgreSQL 业务只读数据库", 503) from None
        return self.engine

    def _configure(self, connection, timeout):
        queries.set_readonly(connection)
        queries.set_statement_timeout(connection, timeout)
        queries.set_lock_timeout(connection, timeout)
        queries.set_catalog_path(connection)
        # The supplied role must have no inherited write/DDL privilege, even outside the allowlist.
        unsafe = queries.has_write_privileges(connection)
        if unsafe:
            raise AppError("SQL_ROLE_NOT_READ_ONLY", "业务连接角色拥有写入或管理权限，请配置专用只读角色", 503)
        # Only ordinary/partitioned tables are supported: views/foreign tables may execute code.
        for table in self.settings.sql_allowed_tables:
            schema, name = table.split(".")
            kind = queries.relation_kind(connection, name, schema)
            if kind != "r":
                raise AppError("SQL_CONFIGURATION_ERROR", "白名单仅支持已存在的普通业务表", 503)

    def schema(self, remaining: float) -> dict:
        started = monotonic()
        try:
            with self._engine().connect() as connection, connection.begin():
                self._configure(connection, min(remaining, self.settings.sql_timeout))
                inspector = queries.schema_inspector(connection)
                tables = {}
                for table in self.settings.sql_allowed_tables:
                    if monotonic() - started >= remaining:
                        raise AppError("SQL_TIMEOUT", "业务数据库结构读取超时", 504)
                    schema, name = table.split(".")
                    tables[table] = [{"name": column["name"], "type": str(column["type"])}
                                     for column in inspector.get_columns(name, schema=schema)]
                return {"source_name": self.settings.sql_source_name, "tables": tables}
        except SQLAlchemyError:
            raise AppError("SQL_UNAVAILABLE", "业务数据库结构读取失败", 502) from None

    def query(self, sql: str, remaining: float) -> dict:
        normalized = sql
        timeout = min(remaining, self.settings.sql_timeout)
        if timeout <= 0:
            raise AppError("SQL_TIMEOUT", "业务查询超时", 504)
        started = monotonic()
        try:
            with self._engine().connect() as connection, connection.begin():
                self._configure(connection, timeout)
                timeout -= monotonic() - started
                if timeout <= 0:
                    raise AppError("SQL_TIMEOUT", "业务查询超时", 504)
                queries.set_query_timeout(connection, timeout)
                result = connection.exec_driver_sql(f"SELECT * FROM ({normalized}) AS bounded_result LIMIT {self.settings.sql_max_rows + 1}", execution_options={"no_parameters": True})
                columns = list(result.keys())
                rows = [list(row) for row in result.fetchmany(self.settings.sql_max_rows + 1)]
        except SQLAlchemyError as error:
            code = "SQL_TIMEOUT" if getattr(error.orig, "sqlstate", None) == "57014" else "SQL_QUERY_FAILED"
            raise AppError(code, "业务查询超时" if code == "SQL_TIMEOUT" else "业务查询失败，请核对获准表结构和查询语法", 504 if code == "SQL_TIMEOUT" else 502) from None
        # Bound individual cells and the whole payload before putting database data into a model.
        truncated = len(rows) > self.settings.sql_max_rows
        bounded, size = [], 0
        for row in rows[:self.settings.sql_max_rows]:
            values = []
            for value in row:
                value = json.loads(json.dumps(value, default=str, ensure_ascii=False))
                if len(json.dumps(value, ensure_ascii=False)) > 1000:
                    value = str(value)[:1000]
                    truncated = True
                values.append(value)
            size += len(json.dumps(values, ensure_ascii=False))
            if size > 16000:
                truncated = True
                break
            bounded.append(values)
        return {"source_name": self.settings.sql_source_name, "query": normalized,
                "columns": columns, "rows": bounded, "truncated": truncated}

    def close(self):
        if self.engine is not None:
            self.engine.dispose()
