"""Opt-in, dedicated PostgreSQL database only; never uses the platform database."""
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, func, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.application.hierarchy import HierarchyService, publish_tree
from app.core.config import Settings
from app.domain.contracts import Chunk, SearchHit
from app.infrastructure.loaders.hierarchy import HierarchyBuilder
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import DocumentRecord, HierarchyNode, User


@pytest.mark.skipif(os.getenv("RUN_HIERARCHY_INTEGRATION") != "1", reason="requires isolated hierarchy PostgreSQL test DB")
def test_atomic_publication_owner_fk_merge_republish_and_cascade():
    raw = os.getenv("HIERARCHY_TEST_DATABASE_URL", "")
    if not raw or "test" not in (make_url(raw).database or "").lower():
        pytest.fail("HIERARCHY_TEST_DATABASE_URL must name a dedicated database containing 'test'")
    settings = Settings(_env_file=None, database_url=raw, hierarchy_enabled=True, database_require_tls=False)
    database = Database(settings)
    database.initialize()
    uid, other, did, other_did = uuid4(), uuid4(), uuid4(), uuid4()
    transaction = database.sessions()
    try:
        with database.sessions() as session:
            session.add_all([User(id=u, username="hierarchy-" + u.hex[:20], password_hash="fixture") for u in (uid, other)])
            session.flush()
            for d, u in ((did, uid), (other_did, other)):
                session.add(DocumentRecord(id=d, user_id=u, original_name="manual.md", file_type=".md", file_path="unused",
                                           content_hash=d.hex * 2, status="processing", chunk_count=0,
                                           embedding_model=settings.embedding_model, parser_metadata={"generation": 1}))
            session.commit()
        leaves = [Chunk(str(uuid4()), str(uid), str(did), i, f"power{i}=100W", "manual.md") for i in range(4)]
        tree = HierarchyBuilder(settings).from_leaves(leaves)
        doc = transaction.get(DocumentRecord, did)
        doc.status = "ready"; doc.chunk_count = len(leaves)
        publish_tree(transaction, doc, tree)
        with database.sessions() as observer:
            assert observer.get(DocumentRecord, did).status == "processing"
            assert observer.scalar(select(func.count()).select_from(HierarchyNode).where(HierarchyNode.document_id == did)) == 0
        transaction.commit()
        merged = HierarchyService(settings, database).expand(uid, [did], [SearchHit(c, .03) for c in leaves])
        assert all(h.parent_context.level == 1 for h in merged)
        assert HierarchyService(settings, database).expand(other, [other_did], []) == []
        with database.sessions() as session:
            with pytest.raises(IntegrityError):
                session.execute(update(HierarchyNode).where(HierarchyNode.node_id == UUID(leaves[0].chunk_id)).values(parent_id=UUID(leaves[1].chunk_id), document_id=other_did, user_id=other))
                session.commit()
            session.rollback()
        # Replacement is idempotent and removes old trees only inside the same transaction.
        publish_tree(transaction, doc, tree); transaction.commit()
        assert transaction.scalar(select(func.count()).select_from(HierarchyNode).where(HierarchyNode.document_id == did)) == len(tree.nodes)
        transaction.execute(delete(DocumentRecord).where(DocumentRecord.id == did));transaction.commit()
        assert transaction.scalar(select(func.count()).select_from(HierarchyNode).where(HierarchyNode.document_id == did)) == 0
    finally:
        transaction.rollback(); transaction.close()
        with database.sessions() as session:
            session.execute(delete(DocumentRecord).where(DocumentRecord.id.in_([did, other_did])))
            session.execute(delete(User).where(User.id.in_([uid, other])))
            session.commit()
        database.close()
