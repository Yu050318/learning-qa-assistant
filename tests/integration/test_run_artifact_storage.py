"""Opt-in actual PostgreSQL artifact migration and receipt atomicity."""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.application.artifacts import ArtifactService
from app.application.capabilities import CapabilityService
from app.application.chat import ChatService
from app.application.runs import RunService
from app.application.sessions import SessionService
from app.application.sandbox import SandboxService
from app.core.config import Settings
from app.core.errors import AppError
from app.domain.capabilities import SandboxPolicy, SkillDeclaration
from app.domain.contracts import AgentChatInput
from app.domain.sandbox import SandboxFile
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import (CapabilityConfiguration, ChatRunOperation,
    ChatRunEvent, ChatRun, RunArtifact, User)
from app.migrate import upgrade
from .test_platform_migrations import database

pytestmark = pytest.mark.skipif(os.getenv('RUN_PLATFORM_MIGRATION_INTEGRATION') != '1', reason='requires isolated PostgreSQL')


@pytest.fixture
def storage(database):
    upgrade(database)
    settings = Settings(_env_file=None, capability_center_enabled=True, chat_runs_enabled=True,
                        tool_approvals_enabled=True, sandbox_enabled=True)
    db = Database(settings)
    db.__dict__['engine'] = database.engine
    with db.sessions() as session:
        user = User(username='artifact-owner', password_hash='preserved-hash', role='admin')
        session.add(user); session.commit(); uid = user.id
    capabilities = CapabilityService(settings, db)
    capabilities.update_runtime(uid, 'sandbox', SandboxPolicy(enabled=True, image='sha256:'+'b'*64), 1)
    capabilities.change(uid, 'skills', 'compute', expected_revision=2, create=True,
        declaration=SkillDeclaration(name='compute', description='Compute', instructions='Use approved Sandbox.',
                                     allowed_tools=('sandbox_execute',)))
    sessions = SessionService(db)
    parent = sessions.create(uid, 'artifact-storage')
    chat = ChatService(settings, db, sessions, SimpleNamespace())
    runs = RunService(settings, db, chat)
    try:
        run = runs.start(uid, parent.id, AgentChatInput(question='calculate', skill_name='compute',
            approved_tools=('sandbox_execute',), approval_catalog_revision=3), 'artifact-storage', uuid4())
        claim = runs.store.claim_next(run.id)
        key = 'tool/artifact-storage'
        runs.store.begin_operation(claim, key, {'name': 'sandbox_execute'}, readonly=False)
        yield SimpleNamespace(settings=settings, db=db, capabilities=capabilities, store=runs.store,
            artifacts=ArtifactService(runs.store, capabilities), claim=claim, key=key, uid=uid, sid=parent.id)
    finally:
        runs.close()


def test_upgrade_retains_legacy_catalog_and_accounts_and_is_repeatable(database):
    upgrade(database, '0009_capability_control')
    uid = uuid4()
    with database.engine.begin() as connection:
        connection.execute(text("INSERT INTO rag.users(id,username,password_hash,role,is_active,created_at,updated_at) VALUES (:id,'old-artifact-owner','preserved-hash','user',true,now(),now())"), {'id': uid})
        before = connection.scalar(text('SELECT configuration FROM rag.capability_configuration WHERE id=1'))
    upgrade(database)
    upgrade(database)
    with database.engine.connect() as connection:
        assert connection.scalar(text('SELECT version_num FROM alembic_version')) == '0010_run_artifacts'
        assert connection.scalar(text('SELECT configuration FROM rag.capability_configuration WHERE id=1')) == before
        assert connection.execute(text('SELECT id,password_hash,role FROM rag.users')).one() == (uid, 'preserved-hash', 'user')
        assert 'run_artifacts' in inspect(connection).get_table_names(schema='rag')
        assert connection.scalar(text('SELECT count(*) FROM rag.run_artifacts')) == 0


def test_bytea_receipts_atomic_event_visibility_and_database_constraints(storage, monkeypatch):
    s = storage
    file = SandboxFile('answer.txt', b'42')
    saved = s.artifacts.save_pending(s.claim, s.key, [file])
    assert s.artifacts.list(s.uid, s.claim.run_id) == []
    original_event = s.store._event
    def fail_event(*args, **kwargs):
        raise AppError('FIXTURE_EVENT_FAILURE', 'Rollback probe', 500)
    monkeypatch.setattr(s.store, '_event', fail_event)
    with pytest.raises(AppError, match='Rollback probe'):
        s.store.complete_operation(s.claim, s.key, {'ok': True, 'value': {}})
    with s.db.sessions() as session:
        assert session.get(ChatRunOperation, (s.claim.run_id, s.key)).state == 'started'
        assert session.scalar(select(RunArtifact.content)) == b'42'
    assert s.artifacts.list(s.uid, s.claim.run_id) == []
    monkeypatch.setattr(s.store, '_event', original_event)
    s.store.complete_operation(s.claim, s.key, {'ok': True, 'value': {}})
    second = ArtifactService(s.store, CapabilityService(s.settings, s.db))
    assert second.list(s.uid, s.claim.run_id) == saved
    assert second.read(s.uid, s.claim.run_id, saved[0]['id'])[1] == b'42'
    with s.db.sessions() as session:
        events = list(session.scalars(select(ChatRunEvent).where(ChatRunEvent.event == 'artifacts_ready')))
        assert len(events) == 1
        row = session.get(RunArtifact, saved[0]['id'])
        assert row.sha256 == file.sha256 and row.size_bytes == 2
    for sql in ("UPDATE rag.run_artifacts SET content=decode('787878','hex')",
                "UPDATE rag.run_artifacts SET operation_key='missing'"):
        with pytest.raises(IntegrityError):
            with s.db.engine.begin() as connection:
                connection.execute(text(sql))
    assert second.read(s.uid, s.claim.run_id, saved[0]['id'])[1] == b'42'


def test_cancel_and_artifact_write_serialize_under_real_run_lock(storage):
    s = storage
    barrier = Barrier(2)
    def write():
        barrier.wait()
        try:
            s.artifacts.save_pending(s.claim, s.key, [SandboxFile('race.txt', b'x')])
            return 'saved'
        except AppError as error:
            assert error.code == 'RUN_CANCEL_REQUESTED'
            return 'cancelled'
    def cancel():
        barrier.wait()
        s.store.request_cancel(s.uid, s.claim.run_id)
        return 'cancelled'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda fn: fn(), [write, cancel]))
    assert results[0] in {'saved', 'cancelled'} and results[1] == 'cancelled'
    assert s.artifacts.list(s.uid, s.claim.run_id) == []
    with s.db.sessions() as session:
        rows = list(session.scalars(select(RunArtifact)))
        assert len(rows) <= 1
        assert session.get(ChatRunOperation, (s.claim.run_id, s.key)).state == 'started'


def test_orphan_cleanup_holds_real_run_lock_and_does_not_rewrite_unknown_receipt(storage):
    s=storage
    runtime=SandboxService(s.settings,s.capabilities,s.artifacts)
    with runtime.cleanup_guard(s.claim.run_id) as allowed:
        assert not allowed
    with s.db.engine.begin() as connection:
        connection.execute(text("UPDATE rag.chat_runs SET lease_expires_at=now()-interval '1 second' WHERE id=:id"),{'id':s.claim.run_id})
    with runtime.cleanup_guard(s.claim.run_id) as allowed:
        assert allowed
        # A concurrent renewal/takeover cannot enter the critical section.
        with s.db.sessions() as other:
            with pytest.raises(OperationalError):
                other.scalar(select(ChatRun).where(ChatRun.id==s.claim.run_id).with_for_update(nowait=True))
            other.rollback()
    with s.db.sessions() as session:
        assert session.get(ChatRunOperation,(s.claim.run_id,s.key)).state=='started'
        assert session.scalar(select(ChatRun).where(ChatRun.id==s.claim.run_id).with_for_update(nowait=True))
