"""Opt-in PostgreSQL migrations; every test uses its own disposable database."""
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.migrate import upgrade
from app.infrastructure.persistence.models import Base

pytestmark = pytest.mark.skipif(os.getenv("RUN_PLATFORM_MIGRATION_INTEGRATION") != "1", reason="requires isolated PostgreSQL")


@pytest.fixture
def database():
    raw = os.getenv("PLATFORM_MIGRATION_TEST_DATABASE_URL", "")
    url = make_url(raw)
    if "test" not in (url.database or "").lower():
        pytest.fail("PLATFORM_MIGRATION_TEST_DATABASE_URL must target a test database")
    name = "parity_test_" + uuid4().hex
    admin = create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(url.set(database=name), hide_parameters=True)
    try:
        yield SimpleNamespace(engine=engine)
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def test_empty_upgrade_and_repeat_are_idempotent(database):
    upgrade(database)
    upgrade(database)
    with database.engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0002_platform_roles"
        assert "role" in {c["name"] for c in inspect(connection).get_columns("users", schema="rag")}
        assert set(inspect(connection).get_table_names(schema="rag")) == {table.name for table in Base.metadata.tables.values()}


def test_unversioned_legacy_adoption_preserves_accounts_and_adds_optional_tables(database):
    upgrade(database, "0001_baseline")
    user_id = uuid4()
    with database.engine.begin() as connection:
        connection.execute(text("INSERT INTO rag.users (id,username,password_hash,is_active,created_at,updated_at) VALUES (:id,'legacy','unchanged-hash',true,now(),now())"), {"id": user_id})
        # Simulate the old bootstrap layout with no Alembic stamp and optional modules absent.
        connection.execute(text("DROP TABLE alembic_version"))
        connection.execute(text("DROP TABLE rag.model_configuration"))
    upgrade(database)
    with database.engine.connect() as connection:
        row = connection.execute(text("SELECT id,password_hash,role FROM rag.users")).one()
        assert row == (user_id, "unchanged-hash", "user")
        assert inspect(connection).has_table("model_configuration", schema="rag")


@pytest.mark.parametrize("change", [
    "ALTER TABLE rag.users DROP COLUMN password_hash",
    "ALTER TABLE rag.users ALTER COLUMN username TYPE varchar(20)",
    "ALTER TABLE rag.documents DROP CONSTRAINT documents_status_check",
    "DROP INDEX rag.ix_documents_user_status; CREATE INDEX ix_documents_user_status ON rag.documents(status)",
])
def test_incompatible_legacy_schema_is_not_stamped(database, change):
    upgrade(database, "0001_baseline")
    with database.engine.begin() as connection:
        connection.execute(text("DROP TABLE alembic_version"))
        connection.exec_driver_sql(change)
    with pytest.raises(RuntimeError, match="Baseline mismatch"):
        upgrade(database)
    with database.engine.connect() as connection:
        assert not inspect(connection).has_table("alembic_version")
        assert "role" not in {c["name"] for c in inspect(connection).get_columns("users", schema="rag")}


def test_role_constraint_and_destructive_downgrade_are_rejected(database):
    from sqlalchemy.exc import IntegrityError
    upgrade(database)
    with pytest.raises(IntegrityError):
        with database.engine.begin() as connection:
            connection.execute(text("INSERT INTO rag.users (id,username,password_hash,is_active,created_at,updated_at,role) VALUES (:id,'invalid','hash',true,now(),now(),'superuser')"), {"id": uuid4()})
    config = Config("alembic.ini")
    with database.engine.begin() as connection:
        config.attributes["connection"] = connection
        with pytest.raises(RuntimeError, match="discard administrator"):
            command.downgrade(config, "0001_baseline")
