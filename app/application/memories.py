from __future__ import annotations

import hashlib
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.errors import AppError
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import UserMemory, utcnow
from app.infrastructure.persistence.repository import Repository


def normalize_memory(content: str) -> str:
    return "\n".join(line.rstrip() for line in content.strip().replace("\r\n", "\n").replace("\r", "\n").split("\n"))


class MemoryService:
    def __init__(self, settings: Settings, database: Database, embeddings, vectors):
        self.settings, self.database, self.embeddings, self.vectors = settings, database, embeddings, vectors

    def list(self, user_id: UUID):
        with self.database.sessions() as database:
            return Repository(database).memories(user_id)

    def create(self, user_id: UUID, content: str, *, created_via: str,
               source_session_id: UUID | None = None, source_message_id: UUID | None = None):
        content = normalize_memory(content)
        if not content or len(content) > 500:
            raise AppError("INVALID_MEMORY", "长期记忆须为 1-500 个字符", 422)
        digest = hashlib.sha256(content.encode()).hexdigest()
        with self.database.sessions() as database:
            repository = Repository(database)
            if repository.memory_count(user_id) >= self.settings.memory_max_items:
                raise AppError("MEMORY_LIMIT_REACHED", "长期记忆数量已达上限，请先删除旧记忆", 409)
            memory = UserMemory(
                user_id=user_id, content=content, content_hash=digest, status="processing",
                created_via=created_via, source_session_id=source_session_id, source_message_id=source_message_id,
            )
            database.add(memory)
            try:
                database.commit()
            except IntegrityError:
                database.rollback()
                raise AppError("DUPLICATE_MEMORY", "相同记忆已经存在", 409) from None
        return self._index(user_id, memory.id)

    def _index(self, user_id: UUID, memory_id: UUID):
        with self.database.sessions() as database:
            memory = Repository(database).memory(user_id, memory_id)
            content, version = memory.content, memory.version
        try:
            vector = self.embeddings.embed_query(content)
            self.vectors.upsert_memory(user_id, memory_id, version, vector)
            with self.database.sessions() as database:
                memory = Repository(database).memory(user_id, memory_id, lock=True)
                if memory.version == version and memory.status == "processing":
                    memory.status, memory.vector_updated_at, memory.last_error = "ready", utcnow(), None
                    database.commit()
                return memory
        except Exception as error:
            with self.database.sessions() as database:
                memory = Repository(database).memory(user_id, memory_id, lock=True)
                if memory.version == version:
                    memory.status, memory.last_error = "failed", type(error).__name__
                    database.commit()
            raise

    def update(self, user_id: UUID, memory_id: UUID, content: str):
        content = normalize_memory(content)
        if not content or len(content) > 500:
            raise AppError("INVALID_MEMORY", "长期记忆须为 1-500 个字符", 422)
        with self.database.sessions() as database:
            memory = Repository(database).memory(user_id, memory_id, lock=True)
            memory.content, memory.content_hash = content, hashlib.sha256(content.encode()).hexdigest()
            memory.version += 1
            memory.status, memory.last_error = "processing", None
            try:
                database.commit()
            except IntegrityError:
                database.rollback()
                raise AppError("DUPLICATE_MEMORY", "相同记忆已经存在", 409) from None
        return self._index(user_id, memory_id)

    def retry(self, user_id: UUID, memory_id: UUID):
        with self.database.sessions() as database:
            memory = Repository(database).memory(user_id, memory_id, lock=True)
            if memory.status not in {"failed", "processing"}:
                raise AppError("INVALID_MEMORY_STATE", "只能重试失败或中断的记忆", 409)
            memory.status, memory.last_error = "processing", None
            database.commit()
        return self._index(user_id, memory_id)

    def delete(self, user_id: UUID, memory_id: UUID) -> None:
        with self.database.sessions() as database:
            memory = Repository(database).memory(user_id, memory_id, lock=True)
            memory.status = "deleting"
            database.commit()
        self.vectors.delete_memory(user_id, memory_id)
        with self.database.sessions() as database:
            memory = Repository(database).memory(user_id, memory_id, lock=True)
            database.delete(memory)
            database.commit()

    def search(self, user_id: UUID, query: str) -> list[dict]:
        with self.database.sessions() as database:
            ready = {item.id: item for item in Repository(database).memories(user_id) if item.status == "ready"}
        if not ready:
            return []
        hits = self.vectors.search_memories(user_id, self.embeddings.embed_query(query), self.settings.memory_top_k)
        results = []
        for hit in hits:
            memory = ready.get(UUID(hit["memory_id"]))
            if memory is None or memory.version != hit["version"]:
                continue
            results.append({"memory_id": memory.id, "content": memory.content, "version": memory.version, "score": hit["score"]})
        return results
