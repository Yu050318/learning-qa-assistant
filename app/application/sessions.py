from contextlib import contextmanager
from threading import Lock
from uuid import UUID

from app.core.errors import AppError
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import ChatSession
from app.infrastructure.persistence.repository import Repository


class SessionService:
    def __init__(self, database: Database):
        self.database = database
        self.locks: dict[UUID, Lock] = {}
        self.locks_guard = Lock()

    @contextmanager
    def exclusive(self, session_id: UUID):
        # ponytail: process-local locks are sufficient until the app runs multiple workers.
        with self.locks_guard:
            lock = self.locks.setdefault(session_id, Lock())
        if not lock.acquire(blocking=False):
            raise AppError("RESOURCE_BUSY", "会话正在处理中，请稍后重试", 409)
        try:
            yield
        finally:
            lock.release()

    def create(self, user_id: UUID, title: str):
        with self.database.sessions() as database:
            session = ChatSession(user_id=user_id, title=title)
            database.add(session)
            database.commit()
            return session

    def list(self, user_id: UUID, offset: int, limit: int, *, query: str = ""):
        with self.database.sessions() as database:
            return Repository(database).chat_sessions(user_id, offset, limit, query=query)

    def rename(self, user_id: UUID, session_id: UUID, title: str):
        with self.exclusive(session_id), self.database.sessions() as database:
            session = Repository(database).chat_session(user_id, session_id, lock=True)
            session.title = title
            database.commit()
            return session

    def get(self, user_id: UUID, session_id: UUID, message_limit: int = 100, message_offset: int = 0):
        with self.database.sessions() as database:
            repository = Repository(database)
            session = repository.chat_session(user_id, session_id)
            messages = repository.messages(user_id, session_id, message_limit, message_offset)
            return session, messages

    def delete(self, user_id: UUID, session_id: UUID):
        with self.database.sessions() as database:
            repository = Repository(database)
            repository.chat_session(user_id, session_id)
            with self.exclusive(session_id):
                session = repository.chat_session(user_id, session_id, lock=True)
                database.delete(session)
                database.commit()
