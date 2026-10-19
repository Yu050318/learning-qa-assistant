from uuid import UUID

from sqlalchemy import func, select, update, or_, inspect
from sqlalchemy.orm import Session

from app.core.errors import AppError, not_found
from app.infrastructure.persistence.models import AuthSession, ChatMessage, ChatSession, DocumentRecord, DocumentSeries, DocumentVersion, User, UserMemory, utcnow


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

    def ready_document_ids(self, user_id: UUID, requested: list[UUID] | None, embedding_model: str, *, current_only: bool | None = None) -> list[UUID]:
        statement = select(DocumentRecord).where(DocumentRecord.user_id == user_id)
        if requested is not None:
            statement = statement.where(DocumentRecord.id.in_(requested))
        elif current_only or current_only is None and inspect(self.database.connection()).has_table("document_versions", schema="rag"):
            statement = statement.where(or_(
                ~select(DocumentVersion.document_id).where(DocumentVersion.document_id == DocumentRecord.id).exists(),
                DocumentRecord.id.in_(select(DocumentSeries.current_document_id).where(DocumentSeries.user_id == user_id)),
            ))
        documents = list(self.database.scalars(statement))
        if requested is not None:
            if {document.id for document in documents} != set(requested):
                raise not_found()
            if any(document.status != "ready" for document in documents):
                raise AppError("DOCUMENT_NOT_READY", "指定文档尚未就绪", 409)
            if any(document.embedding_model != embedding_model for document in documents):
                raise AppError("EMBEDDING_MODEL_MISMATCH", "指定文档使用了不同的向量模型，请重新入库", 409)
        return [document.id for document in documents if document.status == "ready" and document.embedding_model == embedding_model]

    def bind_document_version(self, user_id: UUID, document: DocumentRecord, base_id: UUID | None = None):
        if base_id is None:
            series = DocumentSeries(id=document.id, user_id=user_id, current_document_id=document.id, next_version=2)
            self.database.add(series)
            self.database.flush()
            self.database.add(DocumentVersion(document_id=document.id, series_id=series.id, version_number=1))
            return
        base = self.document(user_id, base_id, lock=True)
        if base.status == "deleting":
            raise AppError("DOCUMENT_DELETING", "该版本正在删除，不能基于它上传新版本", 409)
        binding = self.database.get(DocumentVersion, base.id)
        if binding is None:
            self.bind_document_version(user_id, base)
            self.database.flush()
            binding = self.database.get(DocumentVersion, base.id)
        series = self.database.scalar(select(DocumentSeries).where(DocumentSeries.id == binding.series_id,
                                                                   DocumentSeries.user_id == user_id).with_for_update(nowait=True))
        if series is None:
            raise not_found()
        number = series.next_version
        series.next_version += 1
        self.database.add(DocumentVersion(document_id=document.id, series_id=series.id, version_number=number))

    def version_info(self, user_id: UUID, documents: list[DocumentRecord]) -> dict[UUID, dict]:
        rows = self.database.execute(select(DocumentVersion, DocumentSeries).join(DocumentSeries, DocumentSeries.id == DocumentVersion.series_id).where(
            DocumentVersion.document_id.in_([document.id for document in documents]), DocumentSeries.user_id == user_id)).all()
        bindings = {version.document_id: (version, series) for version, series in rows}
        result = {}
        for document in documents:
            version, series = bindings.get(document.id, (None, None))
            result[document.id] = {"series_id": series.id if series else document.id,
                                   "version_number": version.version_number if version else 1,
                                   "current_document_id": series.current_document_id if series else document.id,
                                   "is_current": series.current_document_id == document.id if series else True,
                                   "content_hash": document.content_hash}
        return result

    def document_versions(self, user_id: UUID, document_id: UUID, offset: int, limit: int):
        document = self.document(user_id, document_id)
        binding = self.database.get(DocumentVersion, document.id)
        if binding is None:
            return ([document] if offset == 0 else []), 1
        statement = select(DocumentRecord).join(DocumentVersion, DocumentRecord.id == DocumentVersion.document_id).join(DocumentSeries, DocumentSeries.id == DocumentVersion.series_id).where(
            DocumentVersion.series_id == binding.series_id, DocumentSeries.user_id == user_id, DocumentRecord.user_id == user_id)
        total = self.database.scalar(select(func.count()).select_from(statement.subquery())) or 0
        return list(self.database.scalars(statement.order_by(DocumentVersion.version_number.desc()).offset(offset).limit(limit))), total

    def version_sources(self, user_id: UUID, document_ids: list[UUID]) -> dict[str, dict]:
        documents = list(self.database.scalars(select(DocumentRecord).where(DocumentRecord.user_id == user_id, DocumentRecord.id.in_(document_ids))))
        if {document.id for document in documents} != set(document_ids):
            raise not_found()
        return {str(document_id): {key: str(info[key]) if key == "series_id" else info[key]
                                  for key in ("series_id", "version_number", "content_hash")}
                for document_id, info in self.version_info(user_id, documents).items()}

    def activate_document_version(self, user_id: UUID, document_id: UUID, expected_current: UUID | None, embedding_model: str):
        document = self.document(user_id, document_id, lock=True)
        if document.status != "ready" or document.embedding_model != embedding_model:
            raise AppError("DOCUMENT_NOT_READY", "只能将当前 Embedding 模型下已就绪的版本设为当前", 409)
        binding = self.database.get(DocumentVersion, document.id)
        if binding is None:
            self.bind_document_version(user_id, document)
            self.database.flush()
            binding = self.database.get(DocumentVersion, document.id)
        series = self.database.scalar(select(DocumentSeries).where(DocumentSeries.id == binding.series_id,
                                                                   DocumentSeries.user_id == user_id).with_for_update(nowait=True))
        if series is None:
            raise not_found()
        if series.current_document_id != expected_current:
            raise AppError("DOCUMENT_VERSION_CONFLICT", "当前版本已改变，请刷新版本列表后重试", 409)
        series.current_document_id = document.id
        return document

    def prepare_version_delete(self, user_id: UUID, document_id: UUID):
        binding = self.database.get(DocumentVersion, document_id)
        if binding is not None:
            series = self.database.scalar(select(DocumentSeries).where(DocumentSeries.id == binding.series_id,
                                                                       DocumentSeries.user_id == user_id).with_for_update(nowait=True))
            if series is None:
                raise not_found()
            if series.current_document_id == document_id:
                series.current_document_id = None

    def finish_version_delete(self, document_id: UUID):
        binding = self.database.get(DocumentVersion, document_id)
        if binding is not None:
            self.database.delete(binding)
            self.database.flush()

    def chat_session(self, user_id: UUID, session_id: UUID, lock: bool = False, *, kind=None) -> ChatSession:
        statement = select(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id == user_id)
        if kind is not None:
            statement = statement.where(ChatSession.kind == kind)
        if lock:
            statement = statement.with_for_update(nowait=True)
        session = self.database.scalar(statement)
        if session is None:
            raise not_found()
        return session

    def chat_sessions(self, user_id: UUID, offset: int = 0, limit: int = 50, *, query: str = "") -> list[ChatSession]:
        statement = select(ChatSession).where(ChatSession.user_id == user_id, ChatSession.kind == "chat")
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
