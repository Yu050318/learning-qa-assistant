"""Opt-in isolated PostgreSQL catalog migration, durability and concurrent CAS."""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from app.application.capabilities import CapabilityService
from app.core.config import Settings
from app.core.errors import AppError
from app.domain.capabilities import HttpToolDeclaration
from app.infrastructure.persistence.models import CapabilityConfigurationVersion, User
from app.migrate import upgrade
from .test_platform_migrations import database

pytestmark = pytest.mark.skipif(os.getenv("RUN_PLATFORM_MIGRATION_INTEGRATION") != "1", reason="requires isolated PostgreSQL")


def test_catalog_upgrade_seeds_once_and_preserves_existing_identity(database):
    upgrade(database, "0008_series_retirement")
    uid = uuid4()
    with database.engine.begin() as connection:
        connection.execute(text("INSERT INTO rag.users(id,username,password_hash,role,is_active,created_at,updated_at) VALUES (:id,'preserved-owner','preserved-hash','user',true,now(),now())"), {"id": uid})
    upgrade(database)
    upgrade(database)
    service = CapabilityService(Settings(_env_file=None, capability_center_enabled=True),
        SimpleNamespace(sessions=sessionmaker(database.engine, expire_on_commit=False)))
    revision, catalog = service.snapshot()
    assert revision == 1 and set(catalog.skills) == {"knowledge", "sql-assistant", "web-research"}
    assert all(item.source == "builtin" and item.version == 1 for item in catalog.skills.values())
    with service.database.sessions() as session:
        assert session.get(User, uid).password_hash == "preserved-hash"
        assert session.get(User, uid).role == "user"
        history = session.scalars(select(CapabilityConfigurationVersion)).all()
        assert len(history) == 1 and history[0].configuration == catalog.model_dump(mode="json")
        assert history[0].created_by is None


def test_managed_sql_policy_probes_real_readonly_database_and_rolls_back_unsafe_role(database):
    import secrets
    import psycopg
    from psycopg import sql
    from sqlalchemy.engine import make_url
    from app.domain.capabilities import SqlPolicy
    from app.infrastructure.sql import ReadOnlySQL
    upgrade(database)
    url = make_url(os.environ["PLATFORM_MIGRATION_TEST_DATABASE_URL"]).set(drivername="postgresql")
    business, role = "agent_test_" + uuid4().hex, "reader_" + uuid4().hex
    password = secrets.token_hex(20)
    with psycopg.connect(url.render_as_string(hide_password=False), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
        try:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(business)))
            try:
                with psycopg.connect(url.set(database=business).render_as_string(hide_password=False), autocommit=True) as owner:
                    owner.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
                    owner.execute("CREATE SCHEMA sales")
                    owner.execute("CREATE TABLE sales.orders (id integer)")
                    owner.execute("INSERT INTO sales.orders VALUES (1),(2),(3)")
                    owner.execute(sql.SQL("GRANT USAGE ON SCHEMA sales TO {}").format(sql.Identifier(role)))
                    owner.execute(sql.SQL("GRANT SELECT ON sales.orders TO {}").format(sql.Identifier(role)))
                    db = SimpleNamespace(sessions=sessionmaker(database.engine, expire_on_commit=False))
                    with db.sessions() as session:
                        user = User(username="runtime-admin", password_hash="fixture", role="admin")
                        session.add(user); session.commit(); uid = user.id
                    settings = Settings(_env_file=None, capability_center_enabled=True,
                        database_url=database.engine.url.render_as_string(hide_password=False),
                        sql_database_url=url.set(database=business, username=role, password=password).render_as_string(hide_password=False))
                    service = CapabilityService(settings, db)
                    policy = SqlPolicy(enabled=True, allowed_tables=("sales.orders",), max_rows=2)
                    service.update_runtime(uid, "sql", policy, 1)
                    execution = service.freeze(uid, search_mode="sql")
                    client = ReadOnlySQL(service.project_sql(settings, execution.sql_policy))
                    try:
                        result = client.query("SELECT id FROM sales.orders ORDER BY id", 5)
                        assert result["rows"] == [[1], [2]] and result["truncated"]
                    finally:
                        client.close()
                    owner.execute(sql.SQL("GRANT UPDATE ON sales.orders TO {}").format(sql.Identifier(role)))
                    with pytest.raises(AppError) as rejected:
                        service.update_runtime(uid, "sql", policy, 2)
                    assert rejected.value.code == "SQL_ROLE_NOT_READ_ONLY"
                    assert service.snapshot()[0] == 2 and len(service.history(uid)["items"]) == 2
                    assert "sql_database_url" not in str(service.control_plane(uid))
            finally:
                admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(business)))
        finally:
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_concurrent_catalog_updates_have_one_winner_and_atomic_history(database):
    upgrade(database)
    db = SimpleNamespace(sessions=sessionmaker(database.engine, expire_on_commit=False))
    with db.sessions() as session:
        user = User(username="catalog-admin", password_hash="fixture", role="admin")
        session.add(user); session.commit(); uid = user.id
    settings = Settings(_env_file=None, capability_center_enabled=True)
    service = CapabilityService(settings, db)
    barrier = Barrier(2)

    def create(name):
        declaration = HttpToolDeclaration(name=name, description="Concurrent fixture", group="public", endpoint="https://api.service.com/lookup")
        barrier.wait(timeout=10)
        try:
            service.change(uid, "tools", name, expected_revision=1, declaration=declaration, create=True)
            return "created", name
        except AppError as error:
            return error.code, name

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, ("public.one", "public.two")))
    assert sorted(result[0] for result in results) == ["CAPABILITY_REVISION_CONFLICT", "created"]
    winner = next(name for outcome, name in results if outcome == "created")
    second = CapabilityService(settings, db)
    revision, catalog = second.snapshot()
    assert revision == 2 and list(catalog.tools) == [winner]
    history = second.history(uid)["items"]
    assert [item["revision"] for item in history] == [2, 1]
    assert history[0]["catalog"] == catalog.model_dump(mode="json")
    assert history[0]["change"] == {"operation": "create", "kind": "tools", "name": winner}
