from __future__ import annotations
from app.manager.transactions import transaction_scope, as_transaction

import hashlib
from uuid import UUID

from app.dao.postgres.errors import IntegrityError

from app.core.config import Settings
from app.core.errors import AppError
from app.entity.time import aware
from app.dao.postgres.database import Database
from app.entity.do.models import UserMemory, utcnow
from app.dao.postgres.repository import Repository


def normalize_memory(content: str) -> str:
    return "\n".join(line.rstrip() for line in content.strip().replace("\r\n", "\n").replace("\r", "\n").split("\n"))


class MemoryService:
    def __init__(self, settings: Settings, database: Database, embeddings, vectors, automatic=None):
        self.settings, self.database, self.embeddings, self.vectors = settings, database, embeddings, vectors
        self.automatic = automatic

    def search_context(self, user_id, query):
        manual = self.search(user_id, query)
        automatic, warnings = self.automatic.search(user_id, query) if self.automatic else ([], [])
        if not automatic:
            return manual, warnings
        # Scores from different indexes are incomparable. Fuse by rank and retain
        # the existing total Top K, rather than silently doubling the evidence budget.
        merged = {}
        for source in (manual, automatic):
            for rank, item in enumerate(source):
                key = normalize_memory(item['content'])
                previous, score = merged.get(key, (item, 0.0))
                merged[key] = (previous, score + 1 / (60 + rank + 1))
        return [dict(item, score=score) for item, score in sorted(merged.values(), key=lambda x: -x[1])[:self.settings.memory_top_k]], warnings

    def list(self, user_id: UUID):
        with transaction_scope(self.database) as database:
            return Repository(database).memories(user_id)

    def create(self, user_id: UUID, content: str, *, created_via: str,
               source_session_id: UUID | None = None, source_message_id: UUID | None = None):
        content = normalize_memory(content)
        if not content or len(content) > 500:
            raise AppError("INVALID_MEMORY", "长期记忆须为 1-500 个字符", 422)
        digest = hashlib.sha256(content.encode()).hexdigest()
        with transaction_scope(self.database) as database:
            repository = Repository(database)
            if repository.memory_count(user_id) >= self.settings.memory_max_items:
                raise AppError("MEMORY_LIMIT_REACHED", "长期记忆数量已达上限，请先删除旧记忆", 409)
            memory = UserMemory(
                user_id=user_id, content=content, content_hash=digest, status="processing",
                created_via=created_via, source_session_id=source_session_id, source_message_id=source_message_id,
            )
            as_transaction(database).save(memory)
            try:
                database.commit()
            except IntegrityError:
                database.rollback()
                raise AppError("DUPLICATE_MEMORY", "相同记忆已经存在", 409) from None
        return self._index(user_id, memory.id)

    def _index(self, user_id: UUID, memory_id: UUID):
        with transaction_scope(self.database) as database:
            memory = Repository(database).memory(user_id, memory_id)
            content, version = memory.content, memory.version
        try:
            vector = self.embeddings.embed_query(content)
            self.vectors.upsert_memory(user_id, memory_id, version, vector)
            with transaction_scope(self.database) as database:
                memory = Repository(database).memory(user_id, memory_id, lock=True)
                if memory.version == version and memory.status == "processing":
                    memory.status, memory.vector_updated_at, memory.last_error = "ready", utcnow(), None
                    database.commit()
                return memory
        except Exception as error:
            with transaction_scope(self.database) as database:
                memory = Repository(database).memory(user_id, memory_id, lock=True)
                if memory.version == version:
                    memory.status, memory.last_error = "failed", type(error).__name__
                    database.commit()
            raise

    def update(self, user_id: UUID, memory_id: UUID, content: str):
        content = normalize_memory(content)
        if not content or len(content) > 500:
            raise AppError("INVALID_MEMORY", "长期记忆须为 1-500 个字符", 422)
        with transaction_scope(self.database) as database:
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
        with transaction_scope(self.database) as database:
            memory = Repository(database).memory(user_id, memory_id, lock=True)
            if memory.status not in {"failed", "processing"}:
                raise AppError("INVALID_MEMORY_STATE", "只能重试失败或中断的记忆", 409)
            memory.status, memory.last_error = "processing", None
            database.commit()
        return self._index(user_id, memory_id)

    def delete(self, user_id: UUID, memory_id: UUID) -> None:
        with transaction_scope(self.database) as database:
            memory = Repository(database).memory(user_id, memory_id, lock=True)
            memory.status = "deleting"
            database.commit()
        self.vectors.delete_memory(user_id, memory_id)
        with transaction_scope(self.database) as database:
            memory = Repository(database).memory(user_id, memory_id, lock=True)
            as_transaction(database).remove(memory)
            database.commit()

    def search(self, user_id: UUID, query: str) -> list[dict]:
        with transaction_scope(self.database) as database:
            ready = {item.id: item for item in Repository(database).memories(user_id) if item.status == "ready"}
        if not ready:
            return []
        hits = self.vectors.search_memories(user_id, self.embeddings.embed_query(query), self.settings.memory_top_k)
        results = []
        for hit in hits:
            memory = ready.get(UUID(hit["memory_id"]))
            if memory is None or memory.version != hit["version"]:
                continue
            results.append({"memory_id": memory.id, "content": memory.content, "version": memory.version,
                            "score": hit["score"], "asserted_at": aware(memory.updated_at).isoformat()})
        return results
