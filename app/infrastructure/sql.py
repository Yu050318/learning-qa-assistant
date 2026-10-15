"""Restricted PostgreSQL access; never use the platform's persistence engine."""

import json
import re
from time import monotonic

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from app.core.errors import AppError


_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
_FUNCTIONS = {
    "COUNT", "SUM", "AVG", "MIN", "MAX", "COALESCE", "NULLIF", "CAST", "TRY_CAST",
    "ROUND", "ABS", "CEIL", "FLOOR", "LOWER", "UPPER", "LENGTH", "CHAR_LENGTH",
    "TRIM", "SUBSTRING", "CONCAT", "DATE_TRUNC", "EXTRACT", "CURRENT_DATE",
    "CURRENT_TIMESTAMP", "CASE", "IF", "GREATEST", "LEAST", "ROW_NUMBER",
    "RANK", "DENSE_RANK", "LAG", "LEAD", "FILTER", "ARRAY_AGG", "STRING_AGG",
    "TIMESTAMP_TRUNC", "GROUP_CONCAT",
}


def validate_query(sql: str, allowed_tables: list[str]) -> str:
    """Accept one SELECT, with only explicit, allowlisted relations/functions."""
    if not sql.strip() or len(sql) > 12000:
        raise AppError("SQL_NOT_ALLOWED", "SQL 为空或过长", 422)
    try:
        statements = sqlglot.parse(sql, read="postgres")
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            raise ValueError
        tree = statements[0]
        forbidden = (exp.DML, exp.DDL, exp.Command, exp.Into, exp.Lock, exp.Dot)
        if any(isinstance(node, forbidden) for node in tree.walk()):
            raise ValueError
        if any(node.args.get("recursive") for node in tree.find_all(exp.With)):
            raise ValueError
        for function in tree.find_all(exp.Func):
            name = function.name.upper() if isinstance(function, exp.Anonymous) else function.sql_name()
            if name not in _FUNCTIONS:
                raise ValueError
        for data_type in tree.find_all(exp.DataType):
            if getattr(data_type.this, "name", str(data_type.this)) not in {
                "BIGINT", "INT", "SMALLINT", "FLOAT", "DOUBLE", "DECIMAL", "BOOLEAN",
                "TEXT", "VARCHAR", "CHAR", "DATE", "TIME", "TIMESTAMP", "TIMESTAMPTZ",
                "TIMETZ", "INTERVAL", "UUID", "JSON", "JSONB",
            }:
                raise ValueError
        for scope in traverse_scope(tree):
            for _, source in scope.selected_sources.values():
                if isinstance(source, exp.Table):
                    if source.catalog or not source.db or not isinstance(source.this, exp.Identifier):
                        raise ValueError
                    if f"{source.db}.{source.name}" not in allowed_tables:
                        raise ValueError
                    # A PostgreSQL parent table may include non-allowlisted descendants.
                    source.set("only", True)
        return tree.sql(dialect="postgres")
    except (sqlglot.errors.SqlglotError, ValueError, KeyError):
        raise AppError("SQL_NOT_ALLOWED", "仅允许白名单表上的单条只读查询与受支持函数", 403) from None


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
                url = make_url(self.settings.sql_database_url.get_secret_value())
                platform = make_url(self.settings.database_url.get_secret_value()) if self.settings.database_url.get_secret_value() else None
                if url.drivername not in {"postgresql", "postgresql+psycopg"}:
                    raise ValueError
                if platform and (url.host, url.port or 5432, url.database) == (platform.host, platform.port or 5432, platform.database):
                    raise ValueError
                self.engine = create_engine(url.set(drivername="postgresql+psycopg"), pool_pre_ping=True,
                                            connect_args={"connect_timeout": max(1, int(self.settings.sql_timeout))},
                                            pool_timeout=self.settings.sql_timeout)
            except (ValueError, SQLAlchemyError):
                raise AppError("SQL_CONFIGURATION_ERROR", "请配置独立的 PostgreSQL 业务只读数据库", 503) from None
        return self.engine

    def _configure(self, connection, timeout):
        connection.execute(text("SET TRANSACTION READ ONLY"))
        connection.execute(text("SELECT set_config('statement_timeout', :ms, true)"), {"ms": str(max(1, int(timeout * 1000)))})
        connection.execute(text("SELECT set_config('lock_timeout', :ms, true)"), {"ms": str(max(1, int(timeout * 1000)))})
        connection.execute(text("SET LOCAL search_path = pg_catalog"))
        # The supplied role must have no inherited write/DDL privilege, even outside the allowlist.
        unsafe = connection.execute(text("""
            SELECT r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication
              OR EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
                AND c.relkind IN ('r','p','v','m','f') AND
                (pg_has_role(current_user, c.relowner, 'USAGE') OR
                 has_table_privilege(current_user,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER')))
              OR EXISTS (SELECT 1 FROM pg_namespace n WHERE n.nspname NOT LIKE 'pg_%'
                AND n.nspname <> 'information_schema' AND has_schema_privilege(current_user,n.oid,'CREATE'))
              OR has_database_privilege(current_user,current_database(),'CREATE')
            FROM pg_roles r WHERE r.rolname=current_user
        """)).scalar_one()
        if unsafe:
            raise AppError("SQL_ROLE_NOT_READ_ONLY", "业务连接角色拥有写入或管理权限，请配置专用只读角色", 503)
        # Only ordinary/partitioned tables are supported: views/foreign tables may execute code.
        for table in self.settings.sql_allowed_tables:
            schema, name = table.split(".")
            kind = connection.execute(text("""SELECT c.relkind FROM pg_class c
                JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=:schema AND c.relname=:name"""),
                {"schema": schema, "name": name}).scalar_one_or_none()
            if kind != "r":
                raise AppError("SQL_CONFIGURATION_ERROR", "白名单仅支持已存在的普通业务表", 503)

    def schema(self, remaining: float) -> dict:
        started = monotonic()
        try:
            with self._engine().connect() as connection, connection.begin():
                self._configure(connection, min(remaining, self.settings.sql_timeout))
                inspector = inspect(connection)
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
        normalized = validate_query(sql, self.settings.sql_allowed_tables)
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
                connection.execute(text("SELECT set_config('statement_timeout', :ms, true)"), {"ms": str(max(1, int(timeout * 1000)))})
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
