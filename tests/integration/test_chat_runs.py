"""Opt-in PostgreSQL run serialization; no LLM or business database involved."""
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.application.runs import RunStore
from app.core.config import Settings
from app.core.errors import AppError
from app.domain.contracts import AgentChatInput
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import User, ChatSession


@pytest.mark.skipif(os.getenv("RUN_CHAT_RUN_INTEGRATION") != "1", reason="requires explicit isolated PostgreSQL chat run test DB")
def test_postgresql_run_lock_and_event_sequences():
    raw = os.getenv("CHAT_RUN_TEST_DATABASE_URL", "")
    if not raw or "test" not in (make_url(raw).database or "").lower():
        pytest.fail("CHAT_RUN_TEST_DATABASE_URL must name an isolated database containing 'test'")
    settings = Settings(_env_file=None, database_url=raw, chat_runs_enabled=True)
    database = Database(settings)
    database.initialize()
    store = RunStore(settings, database)
    uid, sid, key = uuid4(), uuid4(), uuid4()
    try:
        with database.sessions() as session:
            session.add(User(id=uid, username=f"runs-{uid.hex[:20]}", password_hash="fixture"));session.flush()
            session.add(ChatSession(id=sid, user_id=uid, title="fixture"));session.commit()
        run, created = store.create(uid, sid, AgentChatInput("fixture", "sql"), "fixture", key)
        assert created and not store.create(uid, sid, AgentChatInput("fixture", "sql"), "retry", key)[1]
        with database.sessions() as holder:
            holder.scalar(select(ChatSession).where(ChatSession.id == sid).with_for_update())
            # A lost POST response must be retryable even during ChatService's
            # preparation/final-save transaction, without creating another Run.
            retried, created_again = store.create(uid, sid, AgentChatInput("fixture", "sql"), "retry-locked", key)
            assert retried.id == run.id and not created_again
            with pytest.raises(DBAPIError) as busy:
                store.create(uid, sid, AgentChatInput("new question", "sql"), "new", uuid4())
            assert getattr(busy.value.orig, "sqlstate", None) == "55P03"
            with pytest.raises(AppError) as conflict:
                store.create(uid, sid, AgentChatInput("different", "sql"), "changed", key)
            assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
            holder.rollback()
        with database.resource_lock("chat", sid):
            with pytest.raises(AppError) as failure:
                with database.resource_lock("chat", sid):
                    pass
            assert failure.value.code == "RESOURCE_BUSY"
        # The same database is accessed through independent Store instances.
        other = RunStore(settings, database)
        store.append(uid, run.id, "answer_start", "")
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(other.append, uid, run.id, "answer_delta", str(index)) for index in range(8)]
            for future in futures:
                future.result(timeout=20)
        other.fail(uid, run.id, AppError("FIXTURE_FAILURE", "fixture"))
        state, rows = store.read(uid, run.id)
        assert state.state == "failed" and [row.sequence for row in rows] == list(range(1, 12))
        with pytest.raises(AppError):
            store.append(uid, run.id, "answer_delta", "late")
    finally:
        with database.sessions() as session:
            session.execute(delete(ChatSession).where(ChatSession.id == sid))
            session.execute(delete(User).where(User.id == uid));session.commit()
        database.close()
