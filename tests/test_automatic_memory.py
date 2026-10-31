import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.errors import AppError
from app.dao.postgres import automatic_memory as dao
from app.entity.do.models import AutomaticMemory, Base, ChatMessage, ChatSession, MemorySyncJob, User, utcnow
from app.manager.openviking import OpenVikingManager
from app.manager.transactions import transaction_scope
from app.service.automatic_memory import AutomaticMemoryService


@pytest.fixture
def memory_system():
    engine = create_engine('sqlite://')
    connection = engine.connect()
    connection.execute(text("ATTACH DATABASE ':memory:' AS rag"))
    Base.metadata.create_all(connection)
    database = SimpleNamespace(sessions=sessionmaker(bind=connection, expire_on_commit=False))
    settings = Settings(_env_file=None, openviking_enabled=True, openviking_api_key='test-gateway-key')
    calls, state = [], {'complete': False, 'fail': False, 'operations': None}
    user, other = uuid4(), uuid4()
    def handle(request):
        calls.append(request)
        if state['fail']:
            return httpx.Response(503, json={'status': 'error'})
        remote_user = request.headers['X-OpenViking-User']
        prefix = f'viking://user/{remote_user}'
        if request.method == 'GET' and '/sessions/' in request.url.path:
            return httpx.Response(404)
        if request.url.path.endswith('/commit'):
            result = {'status': 'accepted', 'task_id': str(uuid4()), 'archive_uri': prefix + '/sessions/test/history/archive_001'}
        elif '/tasks/' in request.url.path:
            result = {'status': 'completed' if state['complete'] else 'running'}
        elif request.url.path.endswith('/content/read'):
            operations = state['operations'] or {'adds': [{'uri': prefix + '/memories/preferences/style.md', 'after': '偏好简洁的中文回答'}]}
            result = json.dumps({'operations': operations}, ensure_ascii=False)
        elif request.url.path.endswith('/search/find'):
            result = {'memories': [{'uri': prefix + '/memories/preferences/style.md', 'score': .8}]}
        else:
            result = {}
        return httpx.Response(200, json={'status': 'ok', 'result': result})
    manager = OpenVikingManager(settings, httpx.MockTransport(handle))
    service = AutomaticMemoryService(settings, database, manager)
    with database.sessions() as session:
        session.add_all([User(id=user, username='alice', password_hash='hash'), User(id=other, username='bob', password_hash='hash')])
        session.commit()
    yield SimpleNamespace(service=service, manager=manager, database=database, settings=settings,
                          user=user, other=other, calls=calls, state=state)
    manager.close(); connection.close(); engine.dispose()


def enqueue(s, *, team=False, evaluation=False, before_consent=False):
    sid, mid, aid = uuid4(), uuid4(), uuid4()
    with transaction_scope(s.database) as transaction:
        transaction.save(ChatSession(id=sid, user_id=s.user, title='fixture', kind='evaluation' if evaluation else 'chat'))
        transaction.save(ChatMessage(id=mid, user_id=s.user, session_id=sid, role='user', content='我偏好简洁的中文回答',
                                     created_at=utcnow() - timedelta(days=1) if before_consent else utcnow()))
        transaction.save(ChatMessage(id=aid, user_id=s.user, session_id=sid, role='assistant', content='已了解'))
        transaction.flush()
        context = SimpleNamespace(user_id=s.user, session_id=sid, source_message_id=mid,
                                  organization_id=uuid4() if team else None, shared_only=team or evaluation)
        dao.enqueue_capture(transaction, context, aid)
        transaction.commit()


def ready(s):
    s.service.set_enabled(s.user, True)
    enqueue(s)
    s.state['complete'] = True
    assert s.service.run_once()
    return s.service.list(s.user)[0]


def test_consent_is_off_by_default_and_never_backfills(memory_system):
    s = memory_system
    assert not s.service.get_settings(s.user)['automatic_enabled']
    enqueue(s)
    assert not s.service.run_once()
    s.service.set_enabled(s.user, True)
    enqueue(s, before_consent=True)
    enqueue(s, team=True)
    enqueue(s, evaluation=True)
    assert not s.service.run_once()
    assert s.calls == []


def test_commit_is_async_and_only_completed_extraction_is_published(memory_system):
    s = memory_system
    s.service.set_enabled(s.user, True); enqueue(s)
    assert s.service.run_once()
    assert s.service.list(s.user) == []
    assert s.service.get_settings(s.user)['pending_jobs'] == 1
    with s.database.sessions() as session:
        row = session.scalar(select(MemorySyncJob)); row.available_at = utcnow(); session.commit()
    s.state['complete'] = True
    assert s.service.run_once()
    items, warnings = s.service.search(s.user, '回答风格')
    assert len(items) == 1 and not warnings
    assert not s.service.search(s.other, '回答风格')[0]
    posted = [json.loads(r.content) for r in s.calls if r.url.path.endswith('/messages')]
    assert posted == [{'role': 'user', 'content': '我偏好简洁的中文回答'}]


def test_identity_and_uri_scope_cannot_be_overridden(memory_system):
    s = memory_system
    ready(s)
    first = s.calls[0]
    assert first.headers['X-OpenViking-User'] == s.user.hex + '-g1'
    for uri in [f'viking://user/{s.other.hex}-g1/memories/preferences/a.md', 'viking://~/memories/preferences/%2e%2e/a.md',
                'viking://resources/a.md', 'viking://~/memories/experiences/a.md']:
        with pytest.raises(AppError, match='记忆来源'):
            s.manager.memory_uri(s.user, 1, uri)


def test_disable_rotates_identity_and_rejects_pending_old_capture(memory_system):
    s = memory_system
    ready(s); enqueue(s)
    result = s.service.set_enabled(s.user, False)
    assert result['generation'] == 2 and not result['automatic_enabled']
    assert s.service.list(s.user) == [] and s.service.search(s.user, '偏好') == ([], [])
    s.service.run_once()  # An old capture cannot publish into the new generation.
    assert s.service.list(s.user) == []


def test_delete_is_immediate_and_tombstone_prevents_resurrection(memory_system):
    s = memory_system
    item = ready(s)
    with pytest.raises(AppError):
        s.service.delete(s.other, item.id)
    s.service.delete(s.user, item.id)
    assert s.service.list(s.user) == []
    assert not s.service.search(s.user, '偏好')[0]
    assert s.service.run_once()
    enqueue(s); assert s.service.run_once()
    assert s.service.list(s.user) == []
    with s.database.sessions() as session:
        assert session.get(AutomaticMemory, item.id).state == 'deleted'


def test_clear_keeps_opt_in_but_drops_old_generation(memory_system):
    s = memory_system
    ready(s)
    assert s.service.clear(s.user)['automatic_enabled']
    assert s.service.list(s.user) == []
    assert s.service.run_once()
    enqueue(s); assert s.service.run_once()
    assert s.service.list(s.user)[0].generation == 2


def test_external_failure_degrades_and_does_not_republish(memory_system):
    s = memory_system
    ready(s); s.state['fail'] = True
    assert s.service.search(s.user, '偏好') == ([], ['OPENVIKING_UNAVAILABLE'])
    item = s.service.list(s.user)[0]
    s.service.delete(s.user, item.id)
    assert s.service.run_once()
    assert s.service.list(s.user) == []
    assert s.service.get_settings(s.user)['pending_jobs'] == 1


def test_unknown_write_outcome_is_not_replayed(memory_system):
    s = memory_system
    s.service.set_enabled(s.user, True); enqueue(s)
    with s.database.sessions() as session:
        row = session.scalar(select(MemorySyncJob)); row.receipt = {**row.receipt, 'phase': 'committing'}; session.commit()
    assert s.service.run_once()
    assert s.calls == []
    assert s.service.get_settings(s.user)['failed_jobs'] == 1


def test_deleted_citation_is_rejected_before_answer_commit(memory_system):
    s = memory_system
    item = ready(s); s.service.delete(s.user, item.id)
    with transaction_scope(s.database) as transaction, pytest.raises(AppError) as error:
        s.service.validate_citations(transaction, s.user, [{'type': 'memory', 'memory_id': str(item.id)}])
    assert error.value.code == 'MEMORY_SCOPE_CHANGED'


def test_purge_waits_for_remote_extraction_before_deleting_files(memory_system):
    s = memory_system
    s.service.set_enabled(s.user, True)
    enqueue(s)
    s.service.run_once()  # Task is accepted remotely but still running.
    s.service.set_enabled(s.user, False)
    with s.database.sessions() as session:
        for job in session.scalars(select(MemorySyncJob)):
            job.available_at = utcnow()
        session.commit()
    s.service.run_once()  # Cancel local publication for the previous generation.
    s.calls.clear()
    s.service.run_once()  # Purge must poll, without reporting success yet.
    assert not any(request.method == 'DELETE' for request in s.calls)
    assert s.service.get_settings(s.user)['pending_jobs'] == 1
    s.state['complete'] = True
    with s.database.sessions() as session:
        for job in session.scalars(select(MemorySyncJob)):
            job.available_at = utcnow()
        session.commit()
    s.service.run_once()
    deletes = [request for request in s.calls if request.method == 'DELETE']
    assert '/sessions/rag-' in deletes[0].url.path
    assert deletes[-1].url.path.endswith('/fs')
    assert s.service.get_settings(s.user)['pending_jobs'] == 0


def test_uncertain_commit_cannot_be_certified_as_physically_deleted(memory_system):
    s = memory_system
    s.service.set_enabled(s.user, True)
    enqueue(s)
    with s.database.sessions() as session:
        job = session.scalar(select(MemorySyncJob))
        job.receipt = {**job.receipt, 'phase': 'committing'}
        job.state = 'uncertain'
        session.commit()
    s.service.clear(s.user)
    s.service.run_once()
    with s.database.sessions() as session:
        purge = session.scalar(select(MemorySyncJob).where(MemorySyncJob.kind == 'purge'))
        assert purge.state == 'uncertain' and purge.error_code == 'MEMORY_CLEANUP_UNCERTAIN'
    assert not any(request.method == 'DELETE' for request in s.calls)
