from app.manager.transactions import transaction_scope, as_transaction
from app.dao.queries import hierarchy_backfill as queries
import argparse
from contextlib import nullcontext
from uuid import UUID

from app.manager.hierarchy import publish_tree
from app.core.config import get_settings
from app.core.errors import AppError
from app.manager.loaders.hierarchy import HierarchyBuilder
from app.dao.postgres.database import Database
from app.dao.postgres.repository import Repository
from app.dao.milvus import MilvusVectorStore
from app.entity.do.models import DocumentIndex, DocumentTask, IngestionJob
from app.service.ingestion_jobs import IngestionQueue

def require_mutable_index(session, document_id):
    if queries.has_immutable_index(document_id, session):
        raise AppError("DOCUMENT_INDEX_IMMUTABLE", "此版本已绑定索引；请构建并发布新版本，不能原地补建层级", 409)


def backfill(settings, database, vectors, user_id, document_id, *, already_locked=False, guard=None):
    if not settings.hierarchy_enabled:
        raise AppError("HIERARCHY_DISABLED", "请先显式初始化节点表并启用 HIERARCHY_ENABLED", 503)
    if already_locked and guard is None:
        raise ValueError("An owned document lock requires an explicit guard")
    with (nullcontext(guard) if already_locked else database.document_lock(document_id)) as guard:
        fields = ('status', 'embedding_model', 'original_name', 'chunk_count', 'content_hash', 'build_fingerprint', 'parser_metadata')
        with transaction_scope(database) as session:
            document = Repository(session).document(user_id, document_id)
            require_mutable_index(session, document_id)
            if document.status != "ready" or document.embedding_model != settings.embedding_model:
                raise AppError("HIERARCHY_STALE", "仅支持当前向量模型下已就绪的文档", 409)
            snapshot = {name: getattr(document, name) for name in fields}
        guard()
        chunks = vectors.read_document_chunks(user_id, document_id, snapshot['chunk_count'])
        if any(c.source_name != snapshot['original_name'] for c in chunks):
            raise AppError("HIERARCHY_SCOPE_MISMATCH", "切片文件名与文档记录不一致", 503)
        tree = HierarchyBuilder(settings).from_leaves(chunks)
        guard()
        with transaction_scope(database) as session:
            document = Repository(session).document(user_id, document_id, lock=True)
            require_mutable_index(session, document_id)
            if any(getattr(document, name) != snapshot[name] for name in fields):
                raise AppError("HIERARCHY_STALE", "读取向量期间文档已改变，请重试", 409)
            publish_tree(session, document, tree)
            guard()
            session.commit()
        return len(tree.nodes)


def enqueue(settings, database, user_id, document_id, *, expected_task_id=None):
    if settings.ingestion_mode != "durable" or not settings.hierarchy_enabled:
        raise AppError("CONFIGURATION_ERROR", "补建任务要求 durable 模式并启用三级分块", 503)
    with transaction_scope(database) as session:
        document = Repository(session).document(user_id, document_id, lock=True)
        if document.organization_id is not None:
            Repository(session).workspace_member(user_id, document.organization_id, lock=True)
        require_mutable_index(session, document_id)
        if document.status != "ready" or document.embedding_model != settings.embedding_model:
            raise AppError("HIERARCHY_STALE", "仅支持当前模型下已就绪的文档", 409)
        job = as_transaction(session).load(IngestionJob, document_id, with_for_update=True)
        if expected_task_id is not None:
            current = as_transaction(session).load(DocumentTask, job.task_id) if job and job.task_id else None
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
