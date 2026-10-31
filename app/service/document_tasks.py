"""Owner-scoped task history and logical retirement followed by fenced cleanup."""
from app.manager.transactions import transaction_scope, as_transaction
from app.dao.queries import document_tasks as queries

from uuid import uuid4

from app.core.errors import AppError, not_found
from app.entity.bo.retrieval_scope import index_connection_identity
from app.entity.do.models import (
    ChatRun, DocumentIndex, DocumentRecord, DocumentSeries, DocumentVersion, DocumentTask, HierarchyNode, IngestionJob, KnowledgeDocument, RunIndexPin,
)
from app.dao.postgres.repository import Repository
from app.entity.query.document_tasks import DocumentTaskQuery


def cleanup_identity(index):
    return {name: getattr(index, name) for name in ("collection_name", "database_name", "embedding_model",
        "embedding_dimension", "connection_sha256", "build_fingerprint", "source_sha256", "leaf_sha256", "leaf_count")}


def task_response(task):
    return {**{name: getattr(task, name) for name in ("id", "document_id", "parent_task_id", "original_name", "organization_id", "kind", "state", "phase",
        "processed_units", "total_units", "attempts", "max_attempts", "error_code", "created_at", "updated_at")},
        "can_retry": getattr(task, "can_retry", False)}


class DocumentTasks:
    def __init__(self, ingestion):
        self.ingestion, self.settings, self.database = ingestion, ingestion.settings, ingestion.database

    def require(self):
        if self.ingestion.queue is None:
            raise AppError("DOCUMENT_TASKS_DISABLED", "文档任务管理需要持久入库 Worker", 503)

    def get(self, session, uid, task_id):
        task = queries.get_task(session, task_id, uid)
        if task is None:
            raise not_found()
        return task

    def scoped_task(self, session, uid, task_id, organization_id):
        self.ingestion.require_workspace(Repository(session), uid, organization_id)
        task = self.get(session, uid, task_id)
        if task.organization_id != organization_id:
            raise not_found()
        return task

    def annotate(self, session, tasks):
        ids = {task.document_id for task in tasks if task.document_id is not None}
        jobs = queries.jobs_by_document(ids, session)
        documents = queries.documents_by_id(ids, session)
        immutable = queries.indexed_document_ids(ids, session)
        for task in tasks:
            job, doc = jobs.get(task.document_id), documents.get(task.document_id)
            task.can_retry = bool(job and doc and job.task_id == task.id and task.state in {"failed", "cancelled"}
                and ((task.kind == "ingest" and doc.status in {"failed", "processing"}
                        and (not self.settings.document_publication_enabled or doc.id in immutable))
                    or (task.kind == "hierarchy" and doc.status == "ready" and doc.id not in immutable)
                    or (task.kind == "delete" and doc.status == "deleting")))
        return tasks

    def list(self, uid, offset=0, limit=50, *, organization_id=None, task_state=None, kind=None, search="", parent_task_id=None):
        self.require()
        with transaction_scope(self.database) as session:
            self.ingestion.require_workspace(Repository(session), uid, organization_id)
            if parent_task_id is not None:
                parent = self.scoped_task(session, uid, parent_task_id, organization_id)
                if parent.kind != "delete_series":
                    raise not_found()
            filters = DocumentTaskQuery(user_id=uid, organization_id=organization_id, offset=offset, limit=limit,
                parent_task_id=parent_task_id, state=task_state, kind=kind, search=search)
            total, rows = queries.page_tasks(session, filters)
            self.annotate(session, rows)
            return {"items": [task_response(row) for row in rows], "total": total}

    def detail(self, uid, task_id, *, organization_id=None):
        self.require()
        with transaction_scope(self.database) as session:
            task = self.scoped_task(session, uid, task_id, organization_id)
            return task_response(self.annotate(session, [task])[0])

    def retire(self, uid, did, *, retry_failed=False, organization_id=None, expected_task_id=None):
        self.require()
        if self.ingestion.publication is None:
            raise AppError("DOCUMENT_PUBLICATION_DISABLED", "版本退休需要启用文档发布流程", 503)
        with transaction_scope(self.database) as session:
            repository = Repository(session)
            document = self.ingestion.workspace_document(repository, uid, did, organization_id, lock=True)
            previous = as_transaction(session).load(IngestionJob, did)
            if previous is not None and previous.kind == "delete" and previous.task_id is not None:
                task = self.get(session, uid, previous.task_id)
                if expected_task_id is not None and task.id != expected_task_id:
                    if task.details.get("retry_of") == str(expected_task_id):
                        return task
                    raise AppError("DOCUMENT_TASK_NOT_RETRYABLE", "任务已被其他操作替代，请刷新任务列表", 409)
                if document.status == "retired" or task.state in {"queued", "running", "succeeded"} or not retry_failed:
                    return task
            index = as_transaction(session).load(DocumentIndex, did)
            if index is None:
                raise AppError("DOCUMENT_INDEX_NOT_ADOPTED", "请先显式绑定旧文档索引，再执行退休", 409)
            repository.prepare_version_delete(uid, did)
            document.status, document.error_message = "deleting", None
            index.state = "retiring"
            queries.remove_shared_bindings(did, session)
            details = {"index": cleanup_identity(index)}
            if expected_task_id is not None:
                details["retry_of"] = str(expected_task_id)
            task = self.ingestion.queue.enqueue(session, document, reset=True, kind="delete", details=details,
                parent_task_id=task.parent_task_id if previous is not None and previous.kind == "delete" and previous.task_id else None)
            document.parser_metadata = {**(document.parser_metadata or {}), "phase": "retiring"}
            session.commit()
            return task

    def retire_series(self, uid, did, *, organization_id=None, expected_version_count=None):
        self.require()
        self.ingestion.require_versions()
        if self.ingestion.publication is None:
            raise AppError("DOCUMENT_PUBLICATION_DISABLED", "整组退休需要启用文档发布流程", 503)
        with transaction_scope(self.database) as session:
            repository = Repository(session)
            document = self.ingestion.workspace_document(repository, uid, did, organization_id)
            # Adopt only the logical V1 relation here; an old physical index
            # still needs explicit verification before any cleanup is queued.
            if as_transaction(session).load(DocumentVersion, did) is None:
                repository.bind_document_version(uid, document)
                session.flush()
            binding = as_transaction(session).load(DocumentVersion, did)
            series = queries.lock_series(binding, session, uid)
            if series is None:
                raise not_found()
            self.ingestion.require_workspace(repository, uid, organization_id, lock=True)
            if series.retirement_task_id is not None:
                return self.get(session, uid, series.retirement_task_id)
            documents = queries.lock_series_documents(series, session)
            if not documents or any(row.user_id != uid or row.organization_id != organization_id for row in documents):
                raise AppError("INVALID_WORKSPACE_SCOPE", "版本组归属不一致，不能退休", 409)
            if expected_version_count is not None and len(documents) != expected_version_count:
                raise AppError("DOCUMENT_VERSION_CONFLICT", "版本数量已变化，请刷新记录后重新确认退休范围", 409)
            pending = [row for row in documents if row.status != "retired"]
            indexes = {row.id: self.ingestion.publication.index(session, row, allow_bound_model=True) for row in pending}
            parent = DocumentTask(id=uuid4(), document_id=did, user_id=uid, organization_id=organization_id,
                original_name=document.original_name, kind="delete_series", state="queued", phase="queued",
                total_units=len(documents), max_attempts=1,
                details={"series_id": str(series.id), "already_retired": len(documents) - len(pending)})
            as_transaction(session).save(parent)
            session.flush()
            series.current_document_id, series.retirement_task_id = None, parent.id
            for row in pending:
                index = indexes[row.id]
                row.status, row.error_message, index.state = "deleting", None, "retiring"
                queries.remove_document_bindings(row, session)
                self.ingestion.queue.enqueue(session, row, reset=True, kind="delete",
                    details={"index": cleanup_identity(index)}, parent_task_id=parent.id)
                row.parser_metadata = {**(row.parser_metadata or {}), "phase": "retiring"}
            self.ingestion.queue.sync_parent(session, parent.id)
            session.commit()
            return parent

    def process_retirement(self, uid, did, *, guard):
        guard()
        with transaction_scope(self.database) as session:
            document = Repository(session).document(uid, did, lock=True)
            if document.status == "retired":
                return
            job = as_transaction(session).load(IngestionJob, did)
            task = self.get(session, uid, job.task_id) if job and job.task_id else None
            index = as_transaction(session).load(DocumentIndex, did)
            if (document.status != "deleting" or index is None or index.state != "retiring" or task is None
                    or task.kind != "delete" or task.details.get("index") != cleanup_identity(index)
                    or index.source_sha256 != document.content_hash or index.build_fingerprint != document.build_fingerprint):
                raise AppError("DOCUMENT_INDEX_CHANGED", "退休任务与原索引身份不一致", 409)
            if index.connection_sha256 != index_connection_identity(self.settings) or index.database_name != self.settings.milvus_database:
                raise AppError("DOCUMENT_INDEX_UNAVAILABLE", "退休索引连接不可用，拒绝清理其他位置", 409)
            pinned = queries.count_active_pins(did, self.ingestion.queue.now(session), session)
            if pinned:
                raise AppError("DOCUMENT_INDEX_PINNED", "等待引用此版本的运行结束后清理", 409)
            self.ingestion.queue.progress(session, did, "cleaning_index", total_units=document.chunk_count)
            location, path, count = cleanup_identity(index), document.file_path, document.chunk_count
            guard()
            session.commit()
        vectors = self.ingestion.vectors
        if (location["collection_name"] != self.settings.milvus_collection
                or location["embedding_model"] != self.settings.embedding_model
                or location["embedding_dimension"] != self.settings.embedding_dimension):
            vectors = vectors.for_collection(location["collection_name"], embedding_model=location["embedding_model"],
                                             embedding_dimension=location["embedding_dimension"])
        guard()
        vectors.delete_document(uid, did)
        guard()
        with transaction_scope(self.database) as session:
            self.ingestion.queue.progress(session, did, "cleaning_files", processed_units=count)
            guard()
            session.commit()
        self.ingestion.file_path(path).unlink(missing_ok=True)
        parsed = (self.settings.parsed_dir / str(uid) / str(did)).resolve()
        if parsed.is_relative_to(self.settings.parsed_dir.resolve()) and parsed.is_dir():
            for child in parsed.iterdir():
                if child.is_file():
                    child.unlink(missing_ok=True)
            parsed.rmdir()
        guard()
        with transaction_scope(self.database) as session:
            document = Repository(session).document(uid, did, lock=True)
            index = as_transaction(session).load(DocumentIndex, did)
            if document.status != "deleting" or index is None or cleanup_identity(index) != location or index.state != "retiring":
                raise AppError("DOCUMENT_INDEX_CHANGED", "清理期间版本身份变化", 409)
            queries.remove_hierarchy(did, session, uid)
            document.status, document.error_message, index.state = "retired", None, "cleaned"
            document.parser_metadata = {**(document.parser_metadata or {}), "phase": "retired"}
            guard()
            session.commit()

    def retry(self, uid, task_id, *, organization_id=None):
        self.require()
        with transaction_scope(self.database) as session:
            task = self.scoped_task(session, uid, task_id, organization_id)
            if task.state not in {"failed", "cancelled"} or task.document_id is None or task.kind == "delete_series":
                raise AppError("DOCUMENT_TASK_NOT_RETRYABLE", "此任务不能重试", 409)
            did, kind = task.document_id, task.kind
            job = as_transaction(session).load(IngestionJob, did)
            if job and job.task_id and job.task_id != task.id:
                current = self.get(session, uid, job.task_id)
                if current.kind == kind and current.details.get("retry_of") == str(task.id):
                    return self.annotate(session, [current])[0]
                raise AppError("DOCUMENT_TASK_NOT_RETRYABLE", "任务已被其他操作替代，请刷新任务列表", 409)
        if kind == "delete":
            replacement = self.retire(uid, did, retry_failed=True, organization_id=organization_id, expected_task_id=task_id)
        elif kind == "ingest":
            self.ingestion.retry(uid, did, organization_id=organization_id, expected_task_id=task_id)
        else:
            from app.service.hierarchy_backfill import enqueue
            enqueue(self.settings, self.database, uid, did, expected_task_id=task_id)
        with transaction_scope(self.database) as session:
            job = as_transaction(session).load(IngestionJob, did)
            current = self.get(session, uid, replacement.id if kind == "delete" else job.task_id)
            return self.annotate(session, [current])[0]
