from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.errors import AppError, not_found
from app.infrastructure.persistence.models import AuthSession, ChatMessage, ChatSession, DocumentRecord, User, UserMemory, utcnow


class Repository:
    def __init__(self, database: Session):
        self.database = database

    def user_by_username(self, username: str) -> User | None:
        return self.database.scalar(select(User).where(User.username == username))

    def active_auth_session(self, user_id: UUID, session_id: UUID, lock: bool = False) -> AuthSession | None:
        statement = select(AuthSession).join(User).where(
            AuthSession.id == session_id, AuthSession.user_id == user_id,
            AuthSession.revoked_at.is_(None), AuthSession.absolute_expires_at > utcnow(), User.is_active.is_(True),
        )
        if lock:
            statement = statement.with_for_update()
        return self.database.scalar(statement)

    def auth_session(self, session_id: UUID, lock: bool = False) -> AuthSession | None:
        statement = select(AuthSession).where(AuthSession.id == session_id)
        if lock:
            statement = statement.with_for_update()
        return self.database.scalar(statement)

    def revoke_user_sessions(self, user_id: UUID) -> None:
        self.database.execute(update(AuthSession).where(
            AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None)
        ).values(revoked_at=utcnow()))

    def document(self, user_id: UUID, document_id: UUID, lock: bool = False) -> DocumentRecord:
        statement = select(DocumentRecord).where(DocumentRecord.id == document_id, DocumentRecord.user_id == user_id)
        if lock:
            statement = statement.with_for_update(nowait=True)
        document = self.database.scalar(statement)
        if document is None:
            raise not_found()
        return document

    def duplicate(self, user_id: UUID, content_hash: str) -> DocumentRecord | None:
        return self.database.scalar(select(DocumentRecord).where(DocumentRecord.user_id == user_id, DocumentRecord.content_hash == content_hash))

    def documents(self, user_id: UUID, offset: int = 0, limit: int = 50) -> list[DocumentRecord]:
        return list(self.database.scalars(select(DocumentRecord).where(DocumentRecord.user_id == user_id).order_by(DocumentRecord.created_at.desc(), DocumentRecord.id).offset(offset).limit(limit)))

    def search_documents(self, user_id: UUID, query: str, status: str | None, offset: int, limit: int):
        filters = [DocumentRecord.user_id == user_id]
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            filters.append(DocumentRecord.original_name.ilike(f"%{escaped}%", escape="\\"))
        if status:
            filters.append(DocumentRecord.status == status)
        statement = select(DocumentRecord).where(*filters)
        total = self.database.scalar(select(func.count()).select_from(statement.subquery())) or 0
        items = list(self.database.scalars(statement.order_by(DocumentRecord.created_at.desc(), DocumentRecord.id).offset(offset).limit(limit)))
        return items, total

    def processing_documents(self) -> list[DocumentRecord]:
        return list(self.database.scalars(select(DocumentRecord).where(DocumentRecord.status == "processing")))

    def ready_document_ids(self, user_id: UUID, requested: list[UUID] | None, embedding_model: str) -> list[UUID]:
        statement = select(DocumentRecord).where(DocumentRecord.user_id == user_id)
        if requested is not None:
            statement = statement.where(DocumentRecord.id.in_(requested))
        documents = list(self.database.scalars(statement))
        if requested is not None:
            if {document.id for document in documents} != set(requested):
                raise not_found()
            if any(document.status != "ready" for document in documents):
                raise AppError("DOCUMENT_NOT_READY", "指定文档尚未就绪", 409)
            if any(document.embedding_model != embedding_model for document in documents):
                raise AppError("EMBEDDING_MODEL_MISMATCH", "指定文档使用了不同的向量模型，请重新入库", 409)
        return [document.id for document in documents if document.status == "ready" and document.embedding_model == embedding_model]

    def chat_session(self, user_id: UUID, session_id: UUID, lock: bool = False) -> ChatSession:
        statement = select(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id == user_id)
        if lock:
            statement = statement.with_for_update(nowait=True)
        session = self.database.scalar(statement)
        if session is None:
            raise not_found()
        return session

    def chat_sessions(self, user_id: UUID, offset: int = 0, limit: int = 50, *, query: str = "") -> list[ChatSession]:
        statement = select(ChatSession).where(ChatSession.user_id == user_id)
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            statement = statement.where(ChatSession.title.ilike(f"%{escaped}%", escape="\\"))
        return list(self.database.scalars(statement.order_by(ChatSession.updated_at.desc(), ChatSession.id).offset(offset).limit(limit)))

    def messages(self, user_id: UUID, session_id: UUID, limit: int = 100, offset: int = 0) -> list[ChatMessage]:
        self.chat_session(user_id, session_id)
        statement = select(ChatMessage).where(ChatMessage.user_id == user_id, ChatMessage.session_id == session_id).order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).offset(offset).limit(limit)
        return list(reversed(list(self.database.scalars(statement))))

    def context_messages(self, user_id: UUID, session_id: UUID) -> list[ChatMessage]:
        session = self.chat_session(user_id, session_id)
        messages = list(self.database.scalars(select(ChatMessage).where(ChatMessage.user_id == user_id, ChatMessage.session_id == session_id).order_by(ChatMessage.created_at, ChatMessage.id)))
        if session.summarized_through:
            for index, message in enumerate(messages):
                if message.id == session.summarized_through:
                    return messages[index + 1:]
        return messages

    def memories(self, user_id: UUID) -> list[UserMemory]:
        return list(self.database.scalars(select(UserMemory).where(UserMemory.user_id == user_id).order_by(UserMemory.updated_at.desc())))

    def memory(self, user_id: UUID, memory_id: UUID, lock: bool = False) -> UserMemory:
        statement = select(UserMemory).where(UserMemory.id == memory_id, UserMemory.user_id == user_id)
        if lock:
            statement = statement.with_for_update(nowait=True)
        memory = self.database.scalar(statement)
        if memory is None:
            raise not_found()
        return memory

    def memory_count(self, user_id: UUID) -> int:
        return self.database.scalar(select(func.count()).select_from(UserMemory).where(UserMemory.user_id == user_id)) or 0
