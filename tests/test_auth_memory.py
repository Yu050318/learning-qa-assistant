import unittest
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.application.auth import AuthService
from app.application.memories import MemoryService
from app.core.config import Settings
from app.core.errors import AppError
from app.infrastructure.persistence.models import Base, User


class Embeddings:
    def embed_query(self, value):
        return [float(len(value)), 1.0]


class Vectors:
    def __init__(self):
        self.rows = {}

    def upsert_memory(self, user_id, memory_id, version, vector):
        self.rows[memory_id] = {"memory_id": str(memory_id), "user_id": str(user_id), "version": version, "score": 1.0}

    def delete_memory(self, user_id, memory_id):
        row = self.rows.get(memory_id)
        if row and row["user_id"] == str(user_id):
            self.rows.pop(memory_id)

    def search_memories(self, user_id, vector, top_k):
        return [row for row in self.rows.values() if row["user_id"] == str(user_id)][:top_k]


class AuthMemoryTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://")
        self.connection = engine.connect()
        self.connection.execute(text("ATTACH DATABASE ':memory:' AS rag"))
        Base.metadata.create_all(self.connection)
        self.database = SimpleNamespace(sessions=sessionmaker(bind=self.connection, expire_on_commit=False))
        self.settings = Settings(
            _env_file=None, jwt_secret="x" * 32, registration_enabled=True,
            embedding_dimension=64, memory_max_items=10,
        )

    def tearDown(self):
        self.connection.close()

    def test_refresh_rotates_and_replay_revokes_the_device(self):
        auth = AuthService(self.settings, self.database)
        user, access, refresh, _ = auth.register("alice", "correct horse battery")
        context = auth.authenticate(access)
        self.assertEqual(user.id, context.user_id)

        _, next_access, next_refresh, _ = auth.refresh(refresh)
        self.assertNotEqual(refresh, next_refresh)
        self.assertEqual(user.id, auth.authenticate(next_access).user_id)

        with self.assertRaises(AppError) as replayed:
            auth.refresh(refresh)
        self.assertEqual("REFRESH_REPLAYED", replayed.exception.code)
        with self.assertRaises(AppError):
            auth.authenticate(next_access)

    def test_memories_are_scoped_by_user_and_version(self):
        with self.database.sessions() as database:
            first = User(username="first", password_hash="hash")
            second = User(username="second", password_hash="hash")
            database.add_all([first, second])
            database.commit()
        service = MemoryService(self.settings, self.database, Embeddings(), Vectors())
        first_memory = service.create(first.id, "喜欢简洁回答", created_via="management_ui")
        service.create(second.id, "喜欢详细回答", created_via="management_ui")

        self.assertEqual([first_memory.id], [item["memory_id"] for item in service.search(first.id, "回答风格")])
        with self.assertRaises(AppError):
            service.update(second.id, first_memory.id, "越权修改")


if __name__ == "__main__":
    unittest.main()
