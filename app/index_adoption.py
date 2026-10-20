"""Explicit legacy-index verification. Never parses remotely or writes vectors."""
import argparse
import hashlib
import json
import sys
from dataclasses import replace
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select

from app.application.document_publication import DocumentPublication, fingerprint, leaf_fingerprint
from app.application.ingestion import IngestionService
from app.application.workers import WorkerRegistry
from app.core.config import Settings
from app.core.errors import AppError
from app.domain.contracts import Chunk, ExcelSource
from app.domain.retrieval_scope import RetrievalIndex, index_connection_identity, retrieval_index_identity
from app.infrastructure.loaders.hierarchy import HierarchyBuilder, digest
from app.infrastructure.loaders.local import LocalDocumentLoader
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import ChatRun, DocumentIndex, DocumentSeries, DocumentVersion, HierarchyNode, IngestionJob, WorkerHeartbeat, utcnow
from app.infrastructure.persistence.repository import Repository
from app.infrastructure.vectorstores.milvus import MilvusVectorStore


def require_maintenance(session):
    now = WorkerRegistry.now(session)
    workers = session.scalar(select(func.count()).select_from(WorkerHeartbeat).where(
        WorkerHeartbeat.seen_at >= now - timedelta(seconds=45)))
    runs = session.scalar(select(func.count()).select_from(ChatRun).where(
        ChatRun.state.in_(("queued", "running", "waiting", "cancelling")), ChatRun.expires_at > now))
    if workers or runs:
        raise AppError("INDEX_ADOPTION_MAINTENANCE_REQUIRED", "请停止 API 与 Worker，并先结束活动 Run；旧检查点不能自动改写为新索引", 409)


def source_hash(ingestion, document):
    path = ingestion.file_path(document.file_path)
    if not path.is_file():
        raise AppError("SOURCE_FILE_MISSING", "旧源文件不存在，不能绑定索引", 409)
    with path.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if actual != document.content_hash:
        raise AppError("DOCUMENT_SOURCE_CHANGED", "旧源文件摘要与文档记录不一致", 409)
    return actual


def expected_leaves(ingestion, document):
    settings = ingestion.settings
    metadata = document.parser_metadata or {}
    if metadata.get("provider") == "mineru":
        cache = ingestion._cache_path(document.user_id, document.id)
        if cache.is_file() and cache.stat().st_size > settings.max_document_characters * 8 + 2_000_000:
            raise AppError("INDEX_ADOPTION_EVIDENCE_INVALID", "旧解析缓存超过核验大小限制", 409)
        documents = ingestion._load_cache(document.user_id, document.id, document.content_hash, metadata.get("document_type"))
        if documents is None:
            raise AppError("INDEX_ADOPTION_EVIDENCE_MISSING", "缺少匹配原源文件摘要的完整解析缓存，请恢复缓存或新建候选版本", 409)
        evidence = "normalized-cache"
    else:
        documents = ingestion.loader.load(ingestion.file_path(document.file_path), document.file_type)
        evidence = "local-source"
    if (not documents or sum(len(row.page_content) for row in documents) > settings.max_document_characters
            or not any(row.page_content.strip() for row in documents)):
        raise AppError("INDEX_ADOPTION_EVIDENCE_INVALID", "旧解析内容为空或超过核验范围", 409)
    manifest = metadata.get("hierarchy")
    mode = manifest.get("mode") if manifest else "flat"
    if mode in {"recursive", "recursive-rows"}:
        tree = HierarchyBuilder(settings).build(documents, document.user_id, document.id, document.original_name)
        if tree.fingerprint != manifest.get("fingerprint") or tree.mode != mode:
            raise AppError("INDEX_ADOPTION_CONFIGURATION_MISMATCH", "递归分块参数与原版本不同，请使用原参数核验", 409)
        chunks = tree.leaves
    elif mode in {"flat", "backfilled-leaves"}:
        chunks = ingestion.loader.chunks(documents, document.user_id, document.id, document.original_name)
    else:
        raise AppError("INDEX_ADOPTION_EVIDENCE_INVALID", "旧分块模式无法核验，请新建候选版本", 409)
    return chunks, {"evidence": evidence, "evidence_sha256": fingerprint([
        {"content": row.page_content, "metadata": row.metadata} for row in documents]),
        "chunking_mode": mode, "chunk_size": settings.chunk_size, "chunk_overlap": settings.chunk_overlap}


def verify_hierarchy(session, document, actual):
    manifest = (document.parser_metadata or {}).get("hierarchy")
    rows = session.scalars(select(HierarchyNode).where(HierarchyNode.document_id == document.id)).all()
    if not manifest:
        if rows:
            raise AppError("INDEX_ADOPTION_EVIDENCE_INVALID", "存在无有效来源清单的旧层级节点", 409)
        return
    if (manifest.get("version") != 1 or manifest.get("source_hash") != document.content_hash
            or manifest.get("generation") != (document.parser_metadata or {}).get("generation", 0)
            or manifest.get("leaf_count") != len(actual) or manifest.get("node_count") != len(rows)
            or any(row.user_id != document.user_id or row.fingerprint != manifest.get("fingerprint")
                or row.content_hash != digest(row.text) for row in rows)):
        raise AppError("INDEX_ADOPTION_EVIDENCE_INVALID", "旧层级清单与原版本不一致", 409)
    leaves = []
    for row in sorted((node for node in rows if node.level == 3), key=lambda node: node.leaf_index):
        meta = row.node_metadata
        leaves.append(Chunk(str(row.node_id), str(row.user_id), str(row.document_id), row.leaf_index, row.text,
            meta.get("source_name"), meta.get("page_number"), meta.get("section"),
            ExcelSource(**meta["excel_source"]) if meta.get("excel_source") else None))
    if leaf_fingerprint(leaves, document.user_id, document.id) != leaf_fingerprint(actual, document.user_id, document.id):
        raise AppError("INDEX_ADOPTION_EVIDENCE_INVALID", "旧层级叶子与原索引片段不一致", 409)


def adopt(ingestion, user_id, document_id, *, apply=False, maintenance=False, expected_leaf_sha256=None):
    """Dry run by default; application preserves original bytes, leaves and pointers."""
    ingestion.require_versions()
    if apply and (not maintenance or expected_leaf_sha256 is None):
        raise AppError("INDEX_ADOPTION_REVIEW_REQUIRED", "写入前需进入维护状态并提供预检得到的叶子摘要", 409)
    with ingestion.exclusive(document_id) as guard, ingestion.database.sessions() as session:
        repository = Repository(session)
        document = repository.document(user_id, document_id, lock=True)
        if document.organization_id is not None:
            repository.workspace_member(user_id, document.organization_id, lock=True)
        if document.status != "ready":
            raise AppError("DOCUMENT_NOT_READY", "仅支持已就绪旧版本的显式索引核验", 409)
        job = session.get(IngestionJob, document.id)
        if job and job.state in {"queued", "running"}:
            raise AppError("RESOURCE_BUSY", "旧文档仍有待执行任务，请先完成或停止任务", 409)
        source_sha256 = source_hash(ingestion, document)
        existing = session.get(DocumentIndex, document.id)
        if existing:
            # Never turn a new candidate into a publication or replace a binding.
            row = DocumentPublication(ingestion.settings, ingestion.vectors).verify_for_publish(session, document)
            if expected_leaf_sha256 is not None and expected_leaf_sha256 != row.leaf_sha256:
                raise AppError("INDEX_ADOPTION_CONFLICT", "核验后的索引摘要已变化", 409)
            guard()
            source_hash(ingestion, document)
            return {"document_id": str(document.id), "status": "already_bound", "state": row.state,
                "leaf_count": row.leaf_count, "leaf_sha256": row.leaf_sha256, "applied": False}
        if document.build_fingerprint != "legacy" or document.embedding_model != ingestion.settings.embedding_model:
            raise AppError("INDEX_ADOPTION_CONFIGURATION_MISMATCH", "仅支持原模型与已确认的旧索引，请使用原配置或新建候选版本", 409)
        expected, provenance = expected_leaves(ingestion, document)
        actual = ingestion.vectors.read_document_chunks(user_id, document_id, document.chunk_count, verify_model=True)
        leaf_sha256 = leaf_fingerprint(actual, user_id, document_id)
        for leaf in actual:
            UUID(leaf.chunk_id)
        if (len(expected) != document.chunk_count or len(actual) != len(expected)
                or any(replace(stored, chunk_id=rebuilt.chunk_id) != rebuilt for stored, rebuilt in zip(actual, expected))):
            raise AppError("INDEX_ADOPTION_CONTENT_MISMATCH", "完整原文分块与旧索引内容、位置或来源不同，请核对原参数", 409)
        try:
            RetrievalIndex(document_id=document.id, user_id=user_id,
                collection_name=ingestion.settings.milvus_collection, database_name=ingestion.settings.milvus_database,
                embedding_model=document.embedding_model, embedding_dimension=ingestion.settings.embedding_dimension,
                connection_sha256=index_connection_identity(ingestion.settings), build_fingerprint=document.build_fingerprint,
                source_sha256=source_sha256, leaf_sha256=leaf_sha256, leaf_count=len(actual))
        except ValueError:
            raise AppError("INDEX_ADOPTION_CONFIGURATION_MISMATCH", "目标索引身份不能用于冻结检索，请核对集合、模型和维度", 409) from None
        verify_hierarchy(session, document, actual)
        if ingestion.settings.hierarchy_enabled and not (document.parser_metadata or {}).get("hierarchy"):
            raise AppError("INDEX_ADOPTION_HIERARCHY_REQUIRED", "请在绑定冻结索引前先为旧版本补建层级", 409)
        if expected_leaf_sha256 is not None and expected_leaf_sha256 != leaf_sha256:
            raise AppError("INDEX_ADOPTION_CONFLICT", "核验后的索引摘要已变化，请重新预检", 409)
        binding = session.get(DocumentVersion, document.id)
        series = session.scalar(select(DocumentSeries).where(DocumentSeries.id == binding.series_id).with_for_update(nowait=True)) if binding else None
        if series and (series.user_id != user_id or series.retirement_task_id is not None):
            raise AppError("DOCUMENT_SERIES_RETIRED", "旧版本组归属异常或已退休，不能绑定", 409)
        state = "published" if series is None or series.current_document_id == document.id else "superseded"
        result = {"document_id": str(document.id), "status": "verified", "state": state,
            "source_sha256": source_sha256, "leaf_count": len(actual), "leaf_sha256": leaf_sha256,
            "index_identity": retrieval_index_identity(ingestion.settings), **provenance, "applied": False}
        guard()
        source_hash(ingestion, document)
        if apply:
            require_maintenance(session)
            if binding is None:
                repository.bind_document_version(user_id, document)
            DocumentPublication(ingestion.settings, ingestion.vectors).reserve(session, document)
            session.flush()
            row = session.get(DocumentIndex, document.id)
            row.state, row.leaf_count, row.leaf_sha256, row.verified_at = state, len(actual), leaf_sha256, utcnow()
            document.parser_metadata = {**(document.parser_metadata or {}), "index_adoption": {
                "version": 1, **provenance, "leaf_sha256": leaf_sha256,
                "index_identity": result["index_identity"], "verified_at": row.verified_at.isoformat()}}
            guard()
            session.commit()
            result["applied"] = True
        return result


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="完整核验并显式绑定单个旧版本索引；默认只读，不调用模型或重写向量")
    parser.add_argument("--user-id", type=UUID, required=True)
    parser.add_argument("--document-id", type=UUID, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--maintenance", action="store_true", help="确认 API 和 Worker 已停机，活动 Run 已结束")
    parser.add_argument("--expected-leaf-sha256", help="上一次只读核验给出的完整叶子摘要")
    parser.add_argument("--chunk-size", type=int, help="原入库分块大小")
    parser.add_argument("--chunk-overlap", type=int, help="原入库分块重叠")
    parser.add_argument("--collection", help="原索引集合")
    parser.add_argument("--embedding-model", help="原向量模型")
    parser.add_argument("--embedding-dimension", type=int, help="原向量维度")
    options = parser.parse_args()
    database = ingestion = None
    try:
        overrides = {key: value for key, value in {"chunk_size": options.chunk_size, "chunk_overlap": options.chunk_overlap,
            "milvus_collection": options.collection, "embedding_model": options.embedding_model,
            "embedding_dimension": options.embedding_dimension}.items() if value is not None}
        settings = Settings(**overrides)
        database = Database(settings)
        database.check()
        ingestion = IngestionService(settings, database, LocalDocumentLoader(settings), None, None, MilvusVectorStore(settings))
        result = adopt(ingestion, options.user_id, options.document_id, apply=options.apply,
            maintenance=options.maintenance, expected_leaf_sha256=options.expected_leaf_sha256)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(json.dumps({"status": "failed", "code": error.code if isinstance(error, AppError) else "INDEX_ADOPTION_FAILED",
            "error_type": type(error).__name__}))
        return 1
    finally:
        if ingestion:
            ingestion.close()
        if database:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
