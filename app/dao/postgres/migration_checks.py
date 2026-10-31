"""Adopt existing tables only after validating the frozen baseline shape."""
import re

import sqlalchemy as sa
from alembic import op
from alembic.migration import MigrationContext


def normalized_sql(value):
    value = re.sub(r"::(?:character varying|text|integer|bigint|boolean|jsonb)(?:\[\])?", "", str(value).lower())
    value = re.sub(r"=\s*any\s*\(array\[(.*?)\]\)", r" in (\1)", value)
    return re.sub(r'[\s()"]', "", value)


def mismatch(table, detail):
    raise RuntimeError(f"Baseline mismatch: rag.{table}: {detail}; no baseline stamp was applied")


def create_or_validate(name, *elements, schema="rag", **options):
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if not inspector.has_table(name, schema=schema):
        return op.create_table(name, *elements, schema=schema, **options)
    expected = sa.Table(name, sa.MetaData(), *elements, schema=schema)
    actual_columns = {column["name"]: column for column in inspector.get_columns(name, schema=schema)}
    context = MigrationContext.configure(connection)
    for column in expected.columns:
        actual = actual_columns.get(column.name)
        if actual is None:
            mismatch(name, f"missing column {column.name}")
        reflected = sa.Column(column.name, actual["type"])
        if context.impl.compare_type(reflected, column) or actual["nullable"] != column.nullable:
            mismatch(name, f"incompatible column {column.name}")
        if column.server_default is not None:
            default = column.server_default.arg
            if isinstance(default, str):
                default = sa.literal(default).compile(dialect=connection.dialect, compile_kwargs={"literal_binds": True})
            if normalized_sql(actual.get("default")) != normalized_sql(default):
                mismatch(name, f"incompatible default for {column.name}")
    if inspector.get_pk_constraint(name, schema=schema)["constrained_columns"] != list(expected.primary_key.columns.keys()):
        mismatch(name, "primary key")
    unique = {tuple(row["column_names"]) for row in inspector.get_unique_constraints(name, schema=schema)}
    foreign = inspector.get_foreign_keys(name, schema=schema)
    checks = {normalized_sql(row["sqltext"]) for row in inspector.get_check_constraints(name, schema=schema)}
    for constraint in expected.constraints:
        if isinstance(constraint, sa.UniqueConstraint) and tuple(constraint.columns.keys()) not in unique:
            mismatch(name, "missing unique constraint")
        if isinstance(constraint, sa.CheckConstraint) and normalized_sql(constraint.sqltext) not in checks:
            mismatch(name, "missing or changed check constraint")
        if isinstance(constraint, sa.ForeignKeyConstraint):
            targets = [element.target_fullname.split(".") for element in constraint.elements]
            if not any(row["constrained_columns"] == list(constraint.columns.keys())
                       and row["referred_schema"] == schema and row["referred_table"] == targets[0][-2]
                       and row["referred_columns"] == [target[-1] for target in targets]
                       and row.get("options", {}).get("ondelete") == constraint.ondelete for row in foreign):
                mismatch(name, "missing or changed foreign key")


def create_index_or_validate(name, table, columns, unique=False, schema="rag", **options):
    existing = next((row for row in sa.inspect(op.get_bind()).get_indexes(table, schema=schema) if row["name"] == name), None)
    if existing is None:
        op.create_index(name, table, columns, unique=unique, schema=schema, **options)
    elif (existing["column_names"] != columns or existing["unique"] != unique
          or normalized_sql(existing.get("dialect_options", {}).get("postgresql_where", ""))
          != normalized_sql(options.get("postgresql_where", ""))):
        mismatch(table, f"incompatible index {name}")
