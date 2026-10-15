import os
import unittest
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.domain.contracts import Chunk, ExcelSource
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
            first_user = User(username=first_external_id[:32], password_hash="hash")
            second_user = User(username=second_external_id[:32], password_hash="hash")
            session.add_all([first_user, second_user])
            session.flush()
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
            user = User(username=external_id[:32], password_hash="hash")
            session.add(user)
            session.flush()
            chat = ChatSession(user_id=user.id, title="cascade")
            session.add(chat)
            session.flush()
            message = ChatMessage(user_id=user.id, session_id=chat.id, role="user", content="hello")
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
            hits = self.vectors.search(first_user, [first_document], "first", first_vector, 10)
            self.assertEqual([str(first_chunk)], [hit.chunk.chunk_id for hit in hits])
        finally:
            self.vectors.delete_document(first_user, first_document)
            self.vectors.delete_document(second_user, second_document)

    def test_milvus_excel_source_survives_json_roundtrip(self):
        with self.vectors.connection() as client:
            if "excel_source" not in self.vectors._validate(client):
                self.skipTest("explicit --excel-provenance upgrade required on the isolated collection")
        uid, did = uuid4(), uuid4()
        source = ExcelSource("Specs", 4, 4, "sheet:Specs:row:4")
        chunk = Chunk(str(uuid4()), str(uid), str(did), 0, "NX-020 power 180 W", "specs.xlsx", excel_source=source)
        vector = [1.] + [0.] * (self.settings.embedding_dimension - 1)
        try:
            self.vectors.upsert([chunk], [vector])
            hits = self.vectors.search(uid, [did], "NX-020", vector, 6)
            self.assertEqual(source, hits[0].chunk.excel_source)
            self.assertEqual([], self.vectors.search(uuid4(), [did], "NX-020", vector, 6))
        finally:
            self.vectors.delete_document(uid, did)

    def test_parent_context_reads_existing_leaf_rows_without_new_schema(self):
        uid, did = uuid4(), uuid4()
        vector = [1.] + [0.] * (self.settings.embedding_dimension - 1)
        chunks = [Chunk(str(uuid4()), str(uid), str(did), index, f"parent context fact {index}", "context.md") for index in range(2)]
        try:
            self.vectors.upsert(chunks, [vector, vector])
            hits = self.vectors.search(uid, [did], "context", vector, 1)
            expanded = self.vectors.expand_context(uid, [did], hits)
            self.assertEqual(hits[0].chunk, expanded[0].chunk)
            self.assertEqual(tuple(chunks), expanded[0].parent_context.children)
            self.assertTrue(expanded[0].parent_context.contains(hits[0].chunk))
        finally:
            self.vectors.delete_document(uid, did)


if __name__ == "__main__":
    unittest.main()
