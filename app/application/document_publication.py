"""Candidate builds are readable only after index verification and publication."""
from dataclasses import asdict
import hashlib
import json

from app.core.errors import AppError
from app.domain.retrieval_scope import RetrievalIndex, retrieval_index_identity, index_connection_identity
from app.infrastructure.persistence.models import DocumentRecord, DocumentIndex, DocumentSeries, DocumentVersion, utcnow
from sqlalchemy import select


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_fingerprint(settings, extension, provider):
    return fingerprint({"protocol": 1, "index": retrieval_index_identity(settings),
        "embedding_connection": settings.dashscope_base_url, "extension": extension, "provider": provider,
        "parser": settings.mineru_model_version if provider == "mineru" else "local-v2-html-xlsx-row-v1",
        "chunk_size": settings.chunk_size, "chunk_overlap": settings.chunk_overlap,
        "hierarchy": settings.hierarchy_enabled, "hierarchy_factors": [4, 2, 1], "encoding": "cl100k_base"})


def leaf_fingerprint(chunks, user_id, document_id):
    rows = sorted(chunks, key=lambda chunk: chunk.chunk_index)
    if (not rows or [row.chunk_index for row in rows] != list(range(len(rows)))
            or len({row.chunk_id for row in rows}) != len(rows)
            or any(row.user_id != str(user_id) or row.document_id != str(document_id) for row in rows)):
        raise AppError("INDEX_VERIFICATION_FAILED", "索引片段归属、数量或位置不符", 409)
    return fingerprint([asdict(row) for row in rows])


class DocumentPublication:
    def __init__(self, settings, vectors):
        self.settings, self.vectors = settings, vectors

    def reserve(self, session, document):
        session.add(DocumentIndex(document_id=document.id, collection_name=self.settings.milvus_collection,
            database_name=self.settings.milvus_database, embedding_model=document.embedding_model,
            embedding_dimension=self.settings.embedding_dimension, connection_sha256=index_connection_identity(self.settings),
            build_fingerprint=document.build_fingerprint, source_sha256=document.content_hash))

    def freeze(self, session, owners, *, lock=False):
        if not owners:
            return ()
        statement = select(DocumentRecord).where(DocumentRecord.id.in_(owners)).order_by(DocumentRecord.id)
        if lock:
            statement = statement.with_for_update()
        documents = session.scalars(statement).all()
        indexes = {row.document_id: row for row in session.scalars(select(DocumentIndex).where(DocumentIndex.document_id.in_(owners)))}
        if len(documents) != len(owners):
            raise AppError("DOCUMENT_SCOPE_CHANGED", "冻结版本已不存在", 409)
        targets = []
        for document in documents:
            row = indexes.get(document.id)
            if (document.user_id != owners[document.id] or document.status != "ready" or row is None
                    or row.state not in {"published", "superseded"} or not row.verified_at
                    or row.source_sha256 != document.content_hash or row.build_fingerprint != document.build_fingerprint
                    or row.embedding_model != document.embedding_model or row.leaf_count != document.chunk_count
                    or row.database_name != self.settings.milvus_database
                    or row.connection_sha256 != index_connection_identity(self.settings)):
                raise AppError("DOCUMENT_SCOPE_CHANGED", "冻结的文档版本或索引已撤销、清理或变更", 409)
            try:
                targets.append(RetrievalIndex(user_id=document.user_id, **{field: getattr(row, field)
                    for field in RetrievalIndex.__dataclass_fields__ if field != "user_id"}))
            except ValueError:
                raise AppError("DOCUMENT_SCOPE_CHANGED", "索引身份记录无效", 409) from None
        return tuple(targets)

    def validate_targets(self, session, targets, *, lock=False):
        owners = {target.document_id: target.user_id for target in targets}
        if len(owners) != len(targets) or self.freeze(session, owners, lock=lock) != tuple(sorted(targets, key=lambda target: target.document_id)):
            raise AppError("DOCUMENT_SCOPE_CHANGED", "冻结的索引版本已变化", 409)

    def index(self, session, document, *, allow_bound_model=False):
        row = session.get(DocumentIndex, document.id)
        if row is None:
            raise AppError("DOCUMENT_INDEX_NOT_ADOPTED", "此版本尚未显式核验并绑定索引", 409)
        if (row.source_sha256 != document.content_hash or row.build_fingerprint != document.build_fingerprint
                or row.embedding_model != document.embedding_model):
            raise AppError("DOCUMENT_INDEX_CHANGED", "文档与索引版本身份不一致", 409)
        if (row.connection_sha256 != index_connection_identity(self.settings)
                or row.database_name != self.settings.milvus_database
                or not allow_bound_model and (row.embedding_model != self.settings.embedding_model
                    or row.embedding_dimension != self.settings.embedding_dimension)):
            raise AppError("DOCUMENT_INDEX_UNAVAILABLE", "当前连接或向量模型不能读取该版本索引", 409)
        return row

    def assert_build(self, session, document):
        row = self.index(session, document)
        expected = build_fingerprint(self.settings, document.file_type, document.parser_metadata.get("provider", "local"))
        if row.build_fingerprint != expected or row.collection_name != self.settings.milvus_collection:
            raise AppError("INDEX_BUILD_CONFIGURATION_CHANGED", "入库参数已变化，请创建新的候选版本", 409)
        if row.state in {"published", "superseded", "retiring", "cleaned"}:
            raise AppError("DOCUMENT_INDEX_IMMUTABLE", "已发布或退休的索引版本不能原地重建", 409)
        row.state = "building"
        return row

    def verify(self, document, chunks):
        expected = leaf_fingerprint(chunks, document.user_id, document.id)
        leaves = self.vectors.read_document_chunks(document.user_id, document.id, len(chunks), verify_model=True)
        if leaf_fingerprint(leaves, document.user_id, document.id) != expected:
            raise AppError("INDEX_VERIFICATION_FAILED", "写入索引与候选片段内容不一致，不能发布", 409)
        return expected

    def verified(self, session, document, count, leaf_sha256):
        row = self.index(session, document)
        row.leaf_count, row.leaf_sha256, row.verified_at = count, leaf_sha256, utcnow()
        version = session.get(DocumentVersion, document.id)
        series = session.get(DocumentSeries, version.series_id) if version else None
        row.state = "published" if series and series.current_document_id == document.id else "verified"

    def verify_for_publish(self, session, document):
        row = self.index(session, document, allow_bound_model=True)
        if row.state not in {"verified", "published", "superseded"} or not row.verified_at or not row.leaf_sha256 or row.leaf_count != document.chunk_count:
            raise AppError("DOCUMENT_INDEX_NOT_VERIFIED", "候选索引尚未完成核验，不能发布", 409)
        vectors = self.vectors
        if (row.collection_name != self.settings.milvus_collection or row.embedding_model != self.settings.embedding_model
                or row.embedding_dimension != self.settings.embedding_dimension):
            vectors = self.vectors.for_collection(row.collection_name, embedding_model=row.embedding_model,
                                                  embedding_dimension=row.embedding_dimension)
        leaves = vectors.read_document_chunks(document.user_id, document.id, row.leaf_count, verify_model=True)
        if leaf_fingerprint(leaves, document.user_id, document.id) != row.leaf_sha256:
            raise AppError("INDEX_VERIFICATION_FAILED", "候选索引在核验后发生变化，不能发布", 409)
        return row
