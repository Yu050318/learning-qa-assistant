"""Explicit, single-document upgrade. Does not call embeddings or mutate Milvus."""
import argparse
from contextlib import nullcontext
from uuid import UUID
from sqlalchemy import inspect

from app.application.hierarchy import publish_tree
from app.core.config import get_settings
from app.core.errors import AppError
from app.infrastructure.loaders.hierarchy import HierarchyBuilder
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.repository import Repository
from app.infrastructure.vectorstores.milvus import MilvusVectorStore
from app.infrastructure.persistence.models import DocumentIndex, DocumentTask, IngestionJob
from app.application.ingestion_jobs import IngestionQueue


def require_mutable_index(session, document_id):
    if inspect(session.connection()).has_table("document_indexes", schema="rag") and session.get(DocumentIndex, document_id) is not None:
        raise AppError("DOCUMENT_INDEX_IMMUTABLE", "此版本已绑定索引；请构建并发布新版本，不能原地补建层级", 409)


def backfill(settings, database, vectors, user_id, document_id, *, already_locked=False, guard=None):
    if not settings.hierarchy_enabled:
        raise AppError("HIERARCHY_DISABLED", "请先显式初始化节点表并启用 HIERARCHY_ENABLED", 503)
    if already_locked and guard is None:
        raise ValueError("An owned document lock requires an explicit guard")
    with (nullcontext(guard) if already_locked else database.document_lock(document_id)) as guard, database.sessions() as session:
        document = Repository(session).document(user_id, document_id, lock=True)
        require_mutable_index(session, document_id)
        if document.status != "ready" or document.embedding_model != settings.embedding_model:
            raise AppError("HIERARCHY_STALE", "仅支持当前向量模型下已就绪的文档", 409)
        guard()
        chunks = vectors.read_document_chunks(user_id, document_id, document.chunk_count)
        if any(c.source_name != document.original_name for c in chunks):
            raise AppError("HIERARCHY_SCOPE_MISMATCH", "切片文件名与文档记录不一致", 503)
        tree = HierarchyBuilder(settings).from_leaves(chunks)
        publish_tree(session, document, tree)
        guard()
        session.commit()
        return len(tree.nodes)


def enqueue(settings, database, user_id, document_id, *, expected_task_id=None):
    if settings.ingestion_mode != "durable" or not settings.hierarchy_enabled:
        raise AppError("CONFIGURATION_ERROR", "补建任务要求 durable 模式并启用三级分块", 503)
    with database.sessions() as session:
        document = Repository(session).document(user_id, document_id, lock=True)
        if document.organization_id is not None:
            Repository(session).workspace_member(user_id, document.organization_id, lock=True)
        require_mutable_index(session, document_id)
        if document.status != "ready" or document.embedding_model != settings.embedding_model:
            raise AppError("HIERARCHY_STALE", "仅支持当前模型下已就绪的文档", 409)
        job = session.get(IngestionJob, document_id, with_for_update=True)
        if expected_task_id is not None:
            current = session.get(DocumentTask, job.task_id) if job and job.task_id else None
            if current and current.id != expected_task_id and current.kind == "hierarchy" and current.details.get("retry_of") == str(expected_task_id):
                return
            if current is None or current.id != expected_task_id or current.kind != "hierarchy" or current.state not in {"failed", "cancelled"}:
                raise AppError("DOCUMENT_TASK_NOT_RETRYABLE", "任务已被其他操作替代，请刷新任务列表", 409)
        if job and job.state in {"queued", "running"}:
            if job.kind != "hierarchy":
                raise AppError("RESOURCE_BUSY", "文档已有其他执行任务", 409)
            return
        IngestionQueue(settings, database).enqueue(session, document, reset=True, kind="hierarchy", details={"retry_of": str(expected_task_id)} if expected_task_id else None)
        session.commit()


def main():
    parser = argparse.ArgumentParser(description="为单个已就绪文档补建层级，保留叶子 ID 和已有向量")
    parser.add_argument("--user-id", type=UUID, required=True)
    parser.add_argument("--document-id", type=UUID, required=True)
    parser.add_argument("--enqueue", action="store_true", help="提交持久任务，由独立 Worker 执行并按已有租约策略重试")
    options = parser.parse_args()
    settings = get_settings()
    database = Database(settings)
    try:
        if options.enqueue:
            enqueue(settings, database, options.user_id, options.document_id)
            print("Hierarchy backfill queued; existing vectors unchanged")
            return 0
        count = backfill(settings, database, MilvusVectorStore(settings), options.user_id, options.document_id)
        print(f"Hierarchy published: {count} nodes; original leaf IDs/vectors preserved")
        return 0
    except AppError as error:
        print(f"Backfill failed: {error.code}: {error.message}")
        return 1
    except Exception as error:
        print(f"Backfill failed: {type(error).__name__}; check service configuration")
        return 1
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
