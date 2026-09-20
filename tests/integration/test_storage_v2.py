import os
import unittest
from uuid import uuid4

from sqlalchemy import delete
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.domain.contracts import Chunk
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import ChatSession, User
from app.infrastructure.persistence.repository import Repository
from app.infrastructure.vectorstores.milvus import MilvusVectorStore


@unittest.skipUnless(os.getenv("RUN_STORAGE_INTEGRATION") == "1", "set RUN_STORAGE_INTEGRATION=1")
class StorageV2IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = Settings()
        cls.database = Database(cls.settings)
        cls.vectors = MilvusVectorStore(cls.settings)
        cls.database.migrate_v2()
        cls.database.migrate_v2()
        cls.vectors.initialize()

    @classmethod
    def tearDownClass(cls):
        cls.database.close()

    def test_postgresql_versions_are_isolated_and_row_lock_is_exclusive(self):
        external_id = f"integration-{uuid4()}"
        with self.database.sessions() as session:
            user = Repository(session).user(external_id)
            v1 = ChatSession(user_id=user.id, title="v1", api_version="v1")
            v2 = ChatSession(user_id=user.id, title="v2", api_version="v2")
            session.add_all([v1, v2])
            session.commit()
            user_id, v1_id, v2_id = user.id, v1.id, v2.id

        first = self.database.sessions()
        second = self.database.sessions()
        try:
            self.assertEqual([v1_id], [item.id for item in Repository(first).chat_sessions(user_id, api_version="v1")])
            self.assertEqual([v2_id], [item.id for item in Repository(first).chat_sessions(user_id, api_version="v2")])
            Repository(first).chat_session(user_id, v2_id, lock=True, api_version="v2")
            with self.assertRaises(DBAPIError) as raised:
                Repository(second).chat_session(user_id, v2_id, lock=True, api_version="v2")
            self.assertEqual("55P03", getattr(raised.exception.orig, "sqlstate", None))
        finally:
            second.rollback()
            first.rollback()
            second.close()
            first.close()
            with self.database.sessions() as cleanup:
                cleanup.execute(delete(ChatSession).where(ChatSession.user_id == user_id))
                cleanup.execute(delete(User).where(User.id == user_id))
                cleanup.commit()

    def test_milvus_search_keeps_user_and_document_scope(self):
        first_user, second_user = uuid4(), uuid4()
        first_document, second_document = uuid4(), uuid4()
        first_chunk, second_chunk = uuid4(), uuid4()
        dimension = self.settings.embedding_dimension
        first_vector = [1.0] + [0.0] * (dimension - 1)
        second_vector = [0.0, 1.0] + [0.0] * (dimension - 2)
        chunks = [
            Chunk(str(first_chunk), str(first_user), str(first_document), 0, "first", "first.txt"),
            Chunk(str(second_chunk), str(second_user), str(second_document), 0, "second", "second.txt"),
        ]
        try:
            self.vectors.upsert(chunks, [first_vector, second_vector])
            hits = self.vectors.search(first_user, [first_document], first_vector, 10)
            self.assertEqual([str(first_chunk)], [hit.chunk.chunk_id for hit in hits])
        finally:
            self.vectors.delete_document(first_user, first_document)
            self.vectors.delete_document(second_user, second_document)


if __name__ == "__main__":
    unittest.main()
