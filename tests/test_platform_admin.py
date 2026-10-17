"""Real auth/session storage: role checks must hold even with an old access token."""
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.application.auth import AuthService
from app.application.model_center import ModelCenter
from app.core.config import Settings
from app.core.errors import AppError
from app.infrastructure.persistence.models import Base, User
from app.main import create_app
from app.migrate import migrate_model_admins


@pytest.fixture
def platform():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as connection:
        connection.execute(text("ATTACH DATABASE ':memory:' AS rag"))
        Base.metadata.create_all(connection)
    settings = Settings(_env_file=None, jwt_secret="x" * 32, registration_enabled=True,
                        admin_invite_code="invite-测试-secret", auth_cookie_secure=False)
    database = SimpleNamespace(sessions=sessionmaker(engine, expire_on_commit=False))
    auth = AuthService(settings, database)
    services = SimpleNamespace(settings=settings, database=database, auth=auth, close=lambda: None)
    with TestClient(create_app(settings, services)) as client:
        yield SimpleNamespace(client=client, database=database, settings=settings, auth=auth)
    engine.dispose()


def signup(s, username="alice", **extra):
    return s.client.post("/api/v2/auth/register", json={"username": username, "password": "correct-password", **extra})


def test_registration_roles_and_invite_validation_do_not_create_failed_accounts(platform):
    s = platform
    regular = signup(s)
    assert regular.status_code == 201 and regular.json()["user"]["role"] == "user"
    for username, code in [("wrong", "bad-code"), ("unicode", "错误密码")]:
        rejected = signup(s, username, admin_code=code)
        assert rejected.status_code == 403 and rejected.json()["error"]["code"] == "INVALID_ADMIN_CODE"
    injected = signup(s, "injected", role="admin")
    assert injected.status_code == 422
    with s.database.sessions() as session:
        assert [user.username for user in session.scalars(select(User))] == ["alice"]
    admin = signup(s, "admin", admin_code="invite-测试-secret")
    assert admin.status_code == 201 and admin.json()["user"]["role"] == "admin"
    assert "invite-测试-secret" not in admin.text
    token = admin.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert s.client.get("/api/v2/auth/me", headers=headers).json()["role"] == "admin"
    refreshed = s.client.post("/api/v2/auth/refresh", headers={"Origin": "http://testserver"})
    assert refreshed.status_code == 200 and refreshed.json()["user"]["role"] == "admin"
    assert s.client.post("/api/v2/auth/login", json={"username": "admin", "password": "correct-password"}).json()["user"]["role"] == "admin"


def test_invite_rotation_empty_configuration_and_registration_disabled(platform):
    s = platform
    from pydantic import SecretStr
    s.settings.admin_invite_code = SecretStr("new-code")
    assert signup(s, "oldcode", admin_code="invite-测试-secret").status_code == 403
    assert signup(s, "newcode", admin_code="new-code").json()["user"]["role"] == "admin"
    s.settings.admin_invite_code = SecretStr("")
    assert signup(s, "disabled", admin_code="new-code").status_code == 403
    assert signup(s, "normal").json()["user"]["role"] == "user"
    s.settings.registration_enabled = False
    assert signup(s, "closed").status_code == 403


def test_admin_api_rechecks_database_role_and_whitelist_is_not_authority(platform):
    s = platform
    ordinary = signup(s).json()
    admin = signup(s, "admin", admin_code="invite-测试-secret").json()
    regular_headers = {"Authorization": f"Bearer {ordinary['access_token']}"}
    admin_headers = {"Authorization": f"Bearer {admin['access_token']}"}
    s.settings.model_admin_usernames = ["alice"]
    assert s.client.get("/api/v2/admin/overview").status_code == 401
    assert s.client.get("/api/v2/admin/overview", headers=regular_headers).status_code == 403
    assert s.client.get("/api/v2/admin/preflight", headers=regular_headers).status_code == 403
    response = s.client.get("/api/v2/admin/overview", headers=admin_headers)
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert not ModelCenter(s.settings, s.database).is_admin(UUID(ordinary["user"]["id"]))
    with s.database.sessions() as session:
        session.get(User, UUID(admin["user"]["id"])).role = "user"
        session.commit()
    assert s.client.get("/api/v2/admin/overview", headers=admin_headers).status_code == 403
    assert s.client.get("/api/v2/auth/me", headers=admin_headers).json()["role"] == "user"


def test_legacy_admin_migration_is_explicit_atomic_and_idempotent(platform):
    s = platform
    signup(s)
    with pytest.raises(ValueError):
        migrate_model_admins(s.database, ["alice", "missing"])
    with s.database.sessions() as session:
        assert session.scalar(select(User)).role == "user"
    assert migrate_model_admins(s.database, ["alice"]) == 1
    assert migrate_model_admins(s.database, ["alice"]) == 0


def test_invitation_guessing_has_a_bounded_attempt_count(platform):
    s = platform
    for _ in range(10):
        assert signup(s, admin_code="bad").status_code == 403
    assert signup(s, admin_code="bad").status_code == 429
