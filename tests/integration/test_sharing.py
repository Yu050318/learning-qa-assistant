"""Explicit PostgreSQL ACL lock test; never targets the configured app database."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.application.sharing import SharingService
from app.core.config import Settings
from app.core.errors import AppError
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import User, DocumentRecord, Organization


@pytest.mark.skipif(os.getenv("RUN_SHARING_INTEGRATION") != "1", reason="requires explicit isolated PostgreSQL sharing test DB")
def test_final_answer_acl_snapshot_blocks_concurrent_revoke():
    raw = os.getenv("SHARING_TEST_DATABASE_URL", "")
    if not raw or "test" not in (make_url(raw).database or "").lower():
        pytest.fail("SHARING_TEST_DATABASE_URL must name an isolated database containing 'test'")
    settings = Settings(_env_file=None, database_url=raw, sharing_enabled=True)
    db = Database(settings);db.initialize()
    service = SharingService(settings, db)
    owner, reader, did = uuid4(), uuid4(), uuid4()
    oid = None
    final_answer = db.sessions()
    try:
        with db.sessions() as session:
            session.add_all([User(id=owner, username=f"owner-{owner.hex[:20]}", password_hash="fixture"), User(id=reader,username=f"reader-{reader.hex[:20]}",password_hash="fixture")])
            session.flush()
            session.add(DocumentRecord(id=did,user_id=owner,original_name="fixture.md",file_type=".md",file_path="fixture-not-a-file",content_hash=did.hex.ljust(64,"0"),status="ready",embedding_model=settings.embedding_model))
            session.commit()
        oid = service.create_organization(owner,"fixture")["id"]
        service.set_member(owner,oid,f"reader-{reader.hex[:20]}","member")
        kid = service.create_base(owner,oid,"fixture")["id"]
        service.bind(owner,kid,did)
        service.set_grant(owner,kid,reader,"viewer")
        assert service.scope(reader,[kid],[did],session=final_answer,lock=True) == {did:owner}
        # Final answer holds read locks through its commit. Revocation must not
        # slip between the final authorization check and the assistant insert.
        with pytest.raises(DBAPIError) as failure:
            service.set_grant(owner,kid,reader,None)
        assert getattr(failure.value.orig,"sqlstate",None) == "55P03"
        final_answer.commit()
        service.set_grant(owner,kid,reader,None)
        with pytest.raises(AppError) as failure:
            service.scope(reader,[kid],[did])
        assert failure.value.status_code == 404
    finally:
        final_answer.rollback();final_answer.close()
        with db.sessions() as session:
            if oid:
                session.execute(delete(Organization).where(Organization.id == oid))
            session.execute(delete(DocumentRecord).where(DocumentRecord.id == did))
            session.execute(delete(User).where(User.id.in_([owner,reader])));session.commit()
        db.close()
