"""Opt-in PostgreSQL lock verification; never targets the configured application database."""

import hashlib
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.core.errors import AppError
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import DocumentRecord, DocumentSeries, DocumentVersion, User
from app.infrastructure.persistence.repository import Repository


@pytest.mark.skipif(os.getenv("RUN_VERSION_INTEGRATION") != "1", reason="requires an explicit isolated PostgreSQL version test database")
def test_postgresql_series_lock_and_stale_activation_are_exclusive():
    raw = os.getenv("VERSION_TEST_DATABASE_URL", "")
    if not raw or "test" not in (make_url(raw).database or "").lower():
        pytest.fail("VERSION_TEST_DATABASE_URL must name an isolated database containing 'test'")
    database = Database(Settings(_env_file=None, database_url=raw, document_versions_enabled=True))
    database.initialize()
    uid, ids = uuid4(), [uuid4() for _ in range(4)]

    def document(index):
        return DocumentRecord(id=ids[index], user_id=uid, original_name=f"v{index}.md", file_type=".md", file_path="fixture-not-a-real-file",
                              content_hash=hashlib.sha256(str(ids[index]).encode()).hexdigest(), status="ready", embedding_model="fixture")

    first, second = database.sessions(), database.sessions()
    try:
        with database.sessions() as setup:
            setup.add(User(id=uid, username=f"versions-{uuid4().hex[:20]}", password_hash="fixture"));setup.flush()
            root, next_document = document(0), document(1)
            setup.add(root);setup.flush();Repository(setup).bind_document_version(uid, root)
            setup.add(next_document);setup.flush();Repository(setup).bind_document_version(uid, next_document, root.id)
            setup.commit()
        third, fourth = document(2), document(3)
        first.add(third);first.flush()
        Repository(first).bind_document_version(uid, third, ids[0])
        second.add(fourth);second.flush()
        # Different base versions: only the shared series lock prevents number reuse.
        with pytest.raises(DBAPIError) as error:
            Repository(second).bind_document_version(uid, fourth, ids[1])
        assert getattr(error.value.orig, "sqlstate", None) == "55P03"
        second.rollback();first.commit()
        Repository(first).activate_document_version(uid, ids[1], ids[0], "fixture");first.commit()
        with pytest.raises(AppError) as error:
            Repository(second).activate_document_version(uid, ids[2], ids[0], "fixture")
        assert error.value.code == "DOCUMENT_VERSION_CONFLICT"
    finally:
        first.rollback();second.rollback();first.close();second.close()
        with database.sessions() as cleanup:
            cleanup.execute(delete(DocumentVersion).where(DocumentVersion.document_id.in_(ids)))
            cleanup.execute(delete(DocumentSeries).where(DocumentSeries.user_id == uid))
            cleanup.execute(delete(DocumentRecord).where(DocumentRecord.user_id == uid))
            cleanup.execute(delete(User).where(User.id == uid))
            cleanup.commit()
        database.close()
