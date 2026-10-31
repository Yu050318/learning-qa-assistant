"""Explicit PostgreSQL checks; only an independently named test database is accepted."""
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.core.errors import AppError
from app.dao.postgres import automatic_memory as dao
from app.dao.postgres.database import Database
from app.dao.postgres.schema_migrations import upgrade
from app.entity.do.models import AutomaticMemory, ChatMessage, ChatSession, MemorySyncJob, User, utcnow
from app.manager.openviking import OpenVikingManager
from app.manager.transactions import transaction_scope
from app.service.automatic_memory import AutomaticMemoryService

pytestmark = pytest.mark.skipif(os.environ.get('RUN_AUTOMATIC_MEMORY_INTEGRATION') != '1', reason='explicit isolated PostgreSQL opt-in')


@pytest.fixture
def storage():
    url = os.environ['AUTOMATIC_MEMORY_TEST_DATABASE_URL']
    if 'test' not in (make_url(url).database or '').lower():
        pytest.fail('Refusing a database without test in its name')
    settings = Settings(_env_file=None, database_url=url, database_require_tls=False,
                        openviking_enabled=True, openviking_api_key='dummy-no-model-access')
    database, users = Database(settings), [uuid4(), uuid4()]
    upgrade(database)
    with database.sessions() as session:
        session.add_all([User(id=uid, username='memory-eval-' + uid.hex[:16], password_hash='unused') for uid in users])
        session.commit()
    try:
        yield database, settings, users
    finally:
        with database.sessions() as session:
            session.execute(delete(ChatMessage).where(ChatMessage.user_id.in_(users)))
            session.execute(delete(ChatSession).where(ChatSession.user_id.in_(users)))
            session.execute(delete(User).where(User.id.in_(users)))
            session.commit()
        database.close()


def test_queue_rollback_and_skip_locked_preserve_user_order(storage):
    database, _, users = storage
    with pytest.raises(RuntimeError), transaction_scope(database) as transaction:
        dao.enqueue(transaction, users[0], 1, 'capture', 'rolled-back', {'phase': 'new'})
        transaction.flush()
        raise RuntimeError('rollback')
    with transaction_scope(database) as transaction:
        assert dao.jobs(transaction, users[0]) == []
        first = dao.enqueue(transaction, users[0], 1, 'capture', 'first', {'phase': 'new'})
        transaction.flush()
        second = dao.enqueue(transaction, users[0], 1, 'capture', 'second', {'phase': 'new'})
        third = dao.enqueue(transaction, users[1], 1, 'capture', 'other-user', {'phase': 'new'})
        transaction.commit()
    with transaction_scope(database) as left, transaction_scope(database) as right:
        a = dao.claim(left)
        b = dao.claim(right)
        assert a.id == first.id and b.id == third.id
        left.commit(); right.commit()
    with transaction_scope(database) as transaction:
        assert dao.claim(transaction) is None  # Same-user work cannot pass a running predecessor.


def test_expired_owner_is_fenced_after_reclaim(storage):
    database, _, users = storage
    with transaction_scope(database) as transaction:
        dao.enqueue(transaction, users[0], 1, 'capture', 'fixture', {'phase': 'new'})
        transaction.commit()
    with transaction_scope(database) as transaction:
        old = dao.claim(transaction); transaction.commit()
    with database.sessions() as session:
        session.get(MemorySyncJob, old.id).lease_until = utcnow() - timedelta(seconds=1)
        session.commit()
    with transaction_scope(database) as transaction:
        new = dao.claim(transaction); transaction.commit()
    assert old.owner_token != new.owner_token
    with transaction_scope(database) as transaction, pytest.raises(AppError) as error:
        dao.owned(transaction, old)
    assert error.value.code == 'MEMORY_LEASE_LOST'


def test_old_generation_cannot_publish_after_clear(storage):
    database, settings, users = storage
    manager = OpenVikingManager(settings)
    service = AutomaticMemoryService(settings, database, manager)
    try:
        service.set_enabled(users[0], True)
        with transaction_scope(database) as transaction:
            dao.enqueue(transaction, users[0], 1, 'capture', 'fixture', {
                'phase': 'waiting', 'session_id': str(uuid4()), 'source_message_id': str(uuid4())})
            transaction.commit()
        with transaction_scope(database) as transaction:
            job = dao.claim(transaction); transaction.commit()
        service.clear(users[0])
        service._publish(job, {'adds': [{'uri': f'viking://user/{users[0].hex}-g1/memories/profile.md', 'after': 'stale'}]})
        with database.sessions() as session:
            assert session.scalar(select(AutomaticMemory).where(AutomaticMemory.user_id == users[0])) is None
            assert session.get(MemorySyncJob, job.id).state == 'cancelled'
    finally:
        manager.close()
