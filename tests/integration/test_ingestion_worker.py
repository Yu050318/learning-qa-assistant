"""Explicit isolated PostgreSQL tests; never use the application's configured DB."""
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url

from app.application.ingestion_jobs import IngestionQueue
from app.core.config import Settings
from app.core.errors import AppError
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import DocumentRecord, IngestionJob, User


@pytest.mark.skipif(os.getenv("RUN_WORKER_INTEGRATION") != "1", reason="requires an explicit isolated PostgreSQL worker test DB")
def test_postgresql_skip_locked_advisory_lock_lease_and_ack():
    raw = os.getenv("WORKER_TEST_DATABASE_URL", "")
    if not raw or "test" not in (make_url(raw).database or "").lower():
        pytest.fail("WORKER_TEST_DATABASE_URL must name an isolated database containing 'test'")
    settings = Settings(_env_file=None, database_url=raw, ingestion_mode="durable")
    database = Database(settings)
    database.initialize()
    queue = IngestionQueue(settings, database)
    uid, ids = uuid4(), [uuid4(), uuid4()]
    holder = database.sessions()
    try:
        with database.sessions() as session:
            session.add(User(id=uid, username=f"worker-{uuid4().hex[:20]}", password_hash="fixture"))
            session.flush()
            for did in ids:
                document = DocumentRecord(id=did, user_id=uid, original_name="fixture.md", file_type=".md",
                    file_path="fixture-not-a-file", content_hash=did.hex.ljust(64, "0"), status="processing", embedding_model="fixture")
                session.add(document);session.flush();queue.enqueue(session, document)
            session.commit()
        holder.scalar(select(IngestionJob).where(IngestionJob.document_id == ids[0]).with_for_update())
        first = queue.claim()
        assert first.document_id == ids[1]  # Does not wait on the other transaction.
        holder.rollback()
        second = queue.claim()
        assert second.document_id == ids[0] and queue.claim() is None
        with database.document_lock(ids[0]) as probe:
            probe()
            with pytest.raises(AppError) as busy:
                with database.document_lock(ids[0]):
                    pass
            assert busy.value.code == "RESOURCE_BUSY"
        with database.document_lock(ids[0]):
            queue.heartbeat(second)
        with database.sessions() as session:
            job = session.get(IngestionJob, ids[0])
            job.lease_expires_at = queue.now(session) - timedelta(seconds=1)
            session.commit()
        with pytest.raises(AppError):
            queue.heartbeat(second)
        replacement = queue.claim()
        assert replacement.document_id == second.document_id and replacement.token != second.token
        with pytest.raises(AppError):
            queue.finish(second)
        with database.document_lock(ids[0]), database.sessions() as session:
            session.get(DocumentRecord, ids[0]).status = "ready"
            session.commit()
            queue.finish(replacement)
        assert queue.status(uid, ids[0])["state"] == "succeeded"
    finally:
        holder.rollback();holder.close()
        with database.sessions() as session:
            session.execute(delete(IngestionJob).where(IngestionJob.document_id.in_(ids)))
            session.execute(delete(DocumentRecord).where(DocumentRecord.user_id == uid))
            session.execute(delete(User).where(User.id == uid))
            session.commit()
        database.close()
