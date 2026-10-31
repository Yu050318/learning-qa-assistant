"""Restricted PostgreSQL access; never use the platform's persistence engine."""
from app.manager.transactions import transaction_scope, as_transaction

import json
import re
from time import monotonic

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope


from app.dao.postgres.errors import SQLAlchemyError

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


from app.dao.business_sql import ReadOnlySQL as _ReadOnlyDriver

class ReadOnlySQL(_ReadOnlyDriver):
    def query(self, sql: str, remaining: float) -> dict:
        return super().query(validate_query(sql, self.settings.sql_allowed_tables), remaining)
