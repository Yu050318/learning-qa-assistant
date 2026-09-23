import os
import unittest
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.domain.contracts import Chunk
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import ChatMessage, ChatSession, User
from app.infrastructure.persistence.repository import Repository
from app.infrastructure.vectorstores.milvus import MilvusVectorStore


@unittest.skipUnless(os.getenv("RUN_STORAGE_INTEGRATION") == "1", "set RUN_STORAGE_INTEGRATION=1")
class StorageV2IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = Settings()
        cls.database = Database(cls.settings)
        cls.vectors = MilvusVectorStore(cls.settings)
        cls.database.initialize()
        cls.vectors.initialize()

    @classmethod
    def tearDownClass(cls):
        cls.database.close()

    def test_postgresql_sessions_are_user_scoped_and_row_lock_is_exclusive(self):
        first_external_id = f"integration-{uuid4()}"
        second_external_id = f"integration-{uuid4()}"
        with self.database.sessions() as session:
            repository = Repository(session)
            first_user = repository.user(first_external_id)
            second_user = repository.user(second_external_id)
            first_chat = ChatSession(user_id=first_user.id, title="first")
            second_chat = ChatSession(user_id=second_user.id, title="second")
            session.add_all([first_chat, second_chat])
            session.commit()
            first_user_id, second_user_id = first_user.id, second_user.id
            first_chat_id, second_chat_id = first_chat.id, second_chat.id

        first = self.database.sessions()
        second = self.database.sessions()
        try:
            self.assertEqual([first_chat_id], [item.id for item in Repository(first).chat_sessions(first_user_id)])
            self.assertEqual([second_chat_id], [item.id for item in Repository(first).chat_sessions(second_user_id)])
            Repository(first).chat_session(first_user_id, first_chat_id, lock=True)
            with self.assertRaises(DBAPIError) as raised:
                Repository(second).chat_session(first_user_id, first_chat_id, lock=True)
            self.assertEqual("55P03", getattr(raised.exception.orig, "sqlstate", None))
        finally:
            second.rollback()
            first.rollback()
            second.close()
            first.close()
            with self.database.sessions() as cleanup:
                cleanup.execute(delete(ChatSession).where(ChatSession.user_id.in_([first_user_id, second_user_id])))
                cleanup.execute(delete(User).where(User.id.in_([first_user_id, second_user_id])))
                cleanup.commit()

    def test_postgresql_session_delete_cascades_messages(self):
        external_id = f"integration-{uuid4()}"
        with self.database.sessions() as session:
            user = Repository(session).user(external_id)
            chat = ChatSession(user_id=user.id, title="cascade")
            session.add(chat)
            session.flush()
            message = ChatMessage(session_id=chat.id, role="user", content="hello")
            session.add(message)
            session.commit()
            user_id, chat_id, message_id = user.id, chat.id, message.id
            session.delete(chat)
            session.commit()
            self.assertIsNone(session.scalar(select(ChatMessage).where(ChatMessage.id == message_id)))
            session.execute(delete(User).where(User.id == user_id))
            session.commit()

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
