import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from pathlib import Path
from threading import Lock
from typing import BinaryIO
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from langchain_core.documents import Document

from app.core.config import Settings
from app.core.errors import AppError, not_found
from app.application.ingestion_jobs import IngestionQueue
from app.application.document_publication import DocumentPublication, build_fingerprint
from app.application.document_tasks import DocumentTasks
from app.application.hierarchy import publish_tree
from app.infrastructure.loaders.hierarchy import HierarchyBuilder
from app.domain.contracts import EmbeddingProvider, VectorStore
from app.infrastructure.loaders.local import LocalDocumentLoader, validate_document_container, validate_upload
from app.infrastructure.loaders.mineru import DOCUMENT_TYPES, MinerUParser
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import DocumentRecord, DocumentIndex, DocumentTask, IngestionJob
from app.infrastructure.persistence.repository import Repository

logger = logging.getLogger("rag")


def prepare_retry_metadata(metadata: dict) -> dict:
    result = dict(metadata)
    restart_codes = {
        "MINERU_SUBMISSION_UNKNOWN", "MINERU_SUBMISSION_FAILED", "MINERU_UPLOAD_FAILED",
        "MINERU_UPLOAD_AUTH_FAILED", "MINERU_UPLOAD_TIMEOUT", "MINERU_RATE_LIMITED",
        "MINERU_UPSTREAM_UNAVAILABLE", "MINERU_PARSE_FAILED", "MINERU_OUTPUT_UNSUPPORTED",
    }
    if result.get("provider") == "mineru" and (
        result.get("error_code") in restart_codes or result.get("phase") == "uploading"
    ):
        result.update(
            generation=int(result.get("generation", 0)) + 1,
            attempt_id=str(uuid4()),
            batch_id=None,
        )
    result.update(phase="validating", failed_phase=None, error_code=None)
    result.pop("retryable", None)
    return result


class IngestionService:
    def __init__(self, settings: Settings, database: Database, loader: LocalDocumentLoader, mineru: MinerUParser | None, embeddings: EmbeddingProvider, vectors: VectorStore):
        self.settings = settings
        self.database = database
        self.loader = loader
        self.mineru = mineru
        self.embeddings = embeddings
        self.vectors = vectors
        self.publication = DocumentPublication(settings, vectors) if settings.document_publication_enabled else None
        self.locks = [Lock() for _ in range(64)]
        self.queue = IngestionQueue(settings, database) if settings.ingestion_mode == "durable" else None
        self.tasks = DocumentTasks(self)
        self.executor = None if self.queue else ThreadPoolExecutor(max_workers=settings.ingestion_workers, thread_name_prefix="ingestion")
        self.scheduled: set[UUID] = set()
        self.schedule_lock = Lock()

    def schedule(self, user_id: UUID, document_id: UUID) -> bool:
        if self.queue:
            # Upload/retry already enqueued atomically; API never executes durable jobs.
            return True
        with self.schedule_lock:
            if document_id in self.scheduled:
                return False
            self.scheduled.add(document_id)
        try:
            self.executor.submit(self._run_scheduled, user_id, document_id)
        except Exception:
            with self.schedule_lock:
                self.scheduled.discard(document_id)
            raise
        return True

    def _run_scheduled(self, user_id: UUID, document_id: UUID) -> None:
        try:
            self.process(user_id, document_id)
        finally:
            with self.schedule_lock:
                self.scheduled.discard(document_id)

    def recover_pending(self) -> int:
        if self.queue:
            return self.queue.reconcile()
        with self.database.sessions() as database:
            documents = Repository(database).processing_documents()
            for document in documents:
                metadata = document.parser_metadata or {}
                if metadata.get("phase") == "uploading":
                    document.parser_metadata = prepare_retry_metadata(metadata)
            database.commit()
        return sum(self.schedule(document.user_id, document.id) for document in documents)

    def close(self) -> None:
        if self.executor:
            self.executor.shutdown(wait=True, cancel_futures=True)

    @contextmanager
    def exclusive(self, document_id: UUID):
        lock = self.locks[document_id.int % len(self.locks)]
        if not lock.acquire(blocking=False):
            raise AppError("RESOURCE_BUSY", "文档正在处理中，请稍后重试", 409)
        try:
            if self.queue:
                with self.database.document_lock(document_id) as probe:
                    yield probe
            else:
                yield lambda: None
        finally:
            lock.release()

    def file_path(self, stored_path: str) -> Path:
        root = self.settings.upload_dir.resolve()
        path = Path(stored_path).resolve()
        if not path.is_relative_to(root) or path == root:
            raise AppError("INVALID_STORAGE_PATH", "文件路径不在受控目录中", 500)
        return path

    def _cache_path(self, user_id: UUID, document_id: UUID) -> Path:
        root = self.settings.parsed_dir.resolve()
        path = (root / str(user_id) / str(document_id) / "normalized.json").resolve()
        if not path.is_relative_to(root) or path == root:
            raise AppError("INVALID_STORAGE_PATH", "解析缓存路径不在受控目录中", 500)
        return path

    def _save_cache(self, user_id: UUID, document_id: UUID, source_hash: str, document_type: str, documents: list[Document]) -> None:
        path = self._cache_path(user_id, document_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        payload = {
            "schema_version": 1, "source_hash": source_hash, "document_type": document_type,
            "documents": [{"page_content": item.page_content, "metadata": item.metadata} for item in documents],
        }
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def _load_cache(self, user_id: UUID, document_id: UUID, source_hash: str, document_type: str) -> list[Document] | None:
        path = self._cache_path(user_id, document_id)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != 1 or payload.get("source_hash") != source_hash or payload.get("document_type") != document_type:
                return None
            return [Document(page_content=item["page_content"], metadata=item.get("metadata") or {}) for item in payload["documents"]]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def require_workspace(self, repository, user_id, organization_id, *, lock=False):
        if organization_id is not None:
            if not self.settings.workspaces_enabled:
                raise AppError("WORKSPACES_DISABLED", "团队空间尚未启用", 503)
            repository.workspace_member(user_id, organization_id, lock=lock)

    def workspace_document(self, repository, user_id, document_id, organization_id, *, lock=False):
        self.require_workspace(repository, user_id, organization_id, lock=lock)
        document = repository.document(user_id, document_id, lock=lock)
        if document.organization_id != organization_id:
            raise not_found()
        return document

    def upload(self, user_id: UUID, filename: str, content_type: str | None, stream: BinaryIO, *, base_document_id: UUID | None = None, organization_id=None) -> DocumentRecord:
        if organization_id is not None:
            with self.database.sessions() as database:
                self.require_workspace(Repository(database), user_id, organization_id)
        if base_document_id is not None:
            self.require_versions()
            self.get(user_id, base_document_id, organization_id=organization_id)
        name, extension = validate_upload(filename, content_type)
        use_mineru = extension in DOCUMENT_TYPES and extension != ".xlsx"
        build = build_fingerprint(self.settings, extension, "mineru" if use_mineru else "local") if self.publication else "legacy"
        if use_mineru and (not self.settings.mineru_enabled or not self.settings.mineru_api_token.get_secret_value()):
            raise AppError("MINERU_NOT_CONFIGURED", "PDF/Office 解析需要启用并配置 MinerU", 503)
        self.settings.upload_dir.mkdir(parents=True, exist_ok=True)
        document_id = uuid4()
        path = self.file_path(str(self.settings.upload_dir / str(user_id) / f"{document_id}{extension}"))
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        saved = False
        try:
            with path.open("xb") as target:
                while block := stream.read(1024 * 1024):
                    size += len(block)
                    if size > self.settings.max_upload_mb * 1024 * 1024:
                        raise AppError("UPLOAD_TOO_LARGE", "文件超过上传大小限制", 413)
                    digest.update(block)
                    target.write(block)
            if size == 0:
                raise AppError("EMPTY_FILE", "上传文件不能为空")
            validate_document_container(path, extension)
            with self.database.sessions() as database:
                repository = Repository(database)
                self.require_workspace(repository, user_id, organization_id, lock=True)
                if repository.duplicate(user_id, digest.hexdigest(), build, organization_id=organization_id):
                    raise AppError("DUPLICATE_DOCUMENT", "相同内容的文档已存在，请查询状态或重试", 409)
                document = DocumentRecord(
                    id=document_id, user_id=user_id, original_name=name, file_type=extension,
                    file_path=str(path), content_hash=digest.hexdigest(), size_bytes=size, status="processing",
                    embedding_model=self.settings.embedding_model, build_fingerprint=build,
                    organization_id=organization_id,
                    parser_metadata={
                        "provider": "mineru" if use_mineru else "local",
                        "parser_version": "xlsx-row-v1" if extension == ".xlsx" else None,
                        "document_type": DOCUMENT_TYPES.get(extension, "text"), "phase": "validating",
                        "generation": 1, "attempt_id": str(uuid4()), "batch_id": None,
                        "model_version": self.settings.mineru_model_version if use_mineru else None,
                        "source_hash": digest.hexdigest(), "normalized_schema_version": None,
                        "parsed_unit_count": None, "unit_kind": None, "failed_phase": None, "error_code": None,
                    },
                )
                database.add(document)
                try:
                    database.flush()
                    if self.settings.document_versions_enabled:
                        repository.bind_document_version(user_id, document, base_document_id)
                    if self.publication:
                        self.publication.reserve(database, document)
                    if self.queue:
                        self.queue.enqueue(database, document)
                    database.commit()
                except IntegrityError:
                    database.rollback()
                    if repository.duplicate(user_id, digest.hexdigest(), build, organization_id=organization_id):
                        raise AppError("DUPLICATE_DOCUMENT", "相同内容的文档已存在", 409) from None
                    if self.settings.document_versions_enabled:
                        raise AppError("DOCUMENT_VERSION_CONFLICT", "版本创建冲突，请刷新后重试", 409) from None
                    raise
                saved = True
                self.annotate_versions(repository, user_id, [document])
                return document
        finally:
            if not saved:
                path.unlink(missing_ok=True)

    def list(self, user_id: UUID, offset: int, limit: int, *, organization_id=None):
        with self.database.sessions() as database:
            repository = Repository(database)
            self.require_workspace(repository, user_id, organization_id)
            documents = repository.documents(user_id, offset, limit, organization_id=organization_id)
            return self.annotate_versions(repository, user_id, documents)

    def search(self, user_id: UUID, query: str, status: str | None, offset: int, limit: int, *, organization_id=None):
        with self.database.sessions() as database:
            repository = Repository(database)
            self.require_workspace(repository, user_id, organization_id)
            documents, total = repository.search_documents(user_id, query, status, offset, limit, organization_id=organization_id)
            return self.annotate_versions(repository, user_id, documents), total

    def get(self, user_id: UUID, document_id: UUID, *, organization_id=None):
        with self.database.sessions() as database:
            repository = Repository(database)
            return self.annotate_versions(repository, user_id, [self.workspace_document(repository, user_id, document_id, organization_id)])[0]

    def require_versions(self):
        if not self.settings.document_versions_enabled:
            raise AppError("DOCUMENT_VERSIONS_DISABLED", "文档版本管理尚未启用", 503)

    def annotate_versions(self, repository, user_id, documents):
        indexes = repository.document_index_info([document.id for document in documents])
        readable = repository.readable_index_ids([document.id for document in documents], require_index=self.settings.document_publication_enabled)
        for document in documents:
            document.index_info = indexes.get(document.id)
            document.index_queryable = document.id in readable
            document.index_bound_retrieval = self.publication is not None
        if self.settings.document_versions_enabled:
            versions = repository.version_info(user_id, documents)
            for document in documents:
                document.version_info = versions[document.id]
        return documents

    def versions(self, user_id: UUID, document_id: UUID, offset: int, limit: int, *, organization_id=None):
        self.require_versions()
        with self.database.sessions() as database:
            repository = Repository(database)
            self.workspace_document(repository, user_id, document_id, organization_id)
            documents, total = repository.document_versions(user_id, document_id, offset, limit)
            return self.annotate_versions(repository, user_id, documents), total

    def activate_version(self, user_id: UUID, document_id: UUID, expected_current: UUID | None, *, organization_id=None):
        self.require_versions()
        with self.exclusive(document_id), self.database.sessions() as database:
            repository = Repository(database)
            candidate = self.workspace_document(repository, user_id, document_id, organization_id, lock=True)
            if self.publication:
                self.publication.verify_for_publish(database, candidate)
            document = repository.activate_document_version(user_id, document_id, expected_current,
                candidate.embedding_model if self.publication else self.settings.embedding_model)
            if self.publication:
                if expected_current is not None and expected_current != document_id:
                    previous = database.get(DocumentIndex, expected_current)
                    if previous is not None:
                        previous.state = "superseded"
                database.get(DocumentIndex, document_id).state = "published"
            database.commit()
            return self.annotate_versions(repository, user_id, [document])[0]

    def retry(self, user_id: UUID, document_id: UUID, *, organization_id=None, expected_task_id=None):
        with self.exclusive(document_id):
            with self.database.sessions() as database:
                document = self.workspace_document(Repository(database), user_id, document_id, organization_id, lock=True)
                if expected_task_id is not None:
                    job = database.get(IngestionJob, document_id, with_for_update=True)
                    current = database.get(DocumentTask, job.task_id) if job and job.task_id else None
                    if current and current.id != expected_task_id and current.kind == "ingest" and current.details.get("retry_of") == str(expected_task_id):
                        return document
                    if current is None or current.id != expected_task_id or current.kind != "ingest" or current.state not in {"failed", "cancelled"}:
                        raise AppError("DOCUMENT_TASK_NOT_RETRYABLE", "任务已被其他操作替代，请刷新任务列表", 409)
                if document.status not in {"failed", "processing"}:
                    raise AppError("INVALID_DOCUMENT_STATE", "只能重试失败或中断的文档", 409)
                if self.publication is not None and database.get(DocumentIndex, document.id) is None:
                    raise AppError("DOCUMENT_INDEX_NOT_ADOPTED", "旧版本没有构建索引绑定，请在版本记录上传新的候选版本", 409)
                if not self.file_path(document.file_path).is_file():
                    raise AppError("SOURCE_FILE_MISSING", "原始文件不存在，请删除记录后重新上传", 409)
                metadata = prepare_retry_metadata(document.parser_metadata or {})
                document.parser_metadata = metadata
                document.status = "processing"
                document.error_message = None
                document.embedding_model = self.settings.embedding_model
                document.chunk_count = 0
                if self.queue:
                    self.queue.enqueue(database, document, reset=True, details={"retry_of": str(expected_task_id)} if expected_task_id else None)
                database.commit()
                self.annotate_versions(Repository(database), user_id, [document])
                return document

    def process(self, user_id: UUID, document_id: UUID, *, guard=lambda: None, already_locked=False, raise_errors=False) -> None:
        try:
            with (nullcontext() if already_locked else self.exclusive(document_id)):
                guard()
                with self.database.sessions() as database:
                    document = Repository(database).document(user_id, document_id, lock=True)
                    if self.queue and document.status == "failed":
                        failed_metadata = document.parser_metadata or {}
                        if not failed_metadata.get("retryable", False):
                            raise AppError(failed_metadata.get("error_code") or "INGESTION_FAILED",
                                           "上次入库已失败，请手动重试", 409)
                        document.status = "processing"
                        document.parser_metadata = prepare_retry_metadata(document.parser_metadata or {})
                    if document.status != "processing":
                        return
                    metadata = dict(document.parser_metadata or {})
                    if self.queue and metadata.get("phase") == "uploading":
                        metadata = prepare_retry_metadata(metadata)
                    generation = int(metadata.get("generation", 0))
                    metadata["phase"] = "parsing" if metadata.get("provider") == "mineru" else "normalizing"
                    document.parser_metadata = metadata
                    if self.queue:
                        self.queue.progress(database, document_id, metadata["phase"], processed_units=0)
                    snapshot = (document.file_path, document.file_type, document.original_name, metadata, document.content_hash)
                    guard()
                    database.commit()
                try:
                    guard()
                    if self.publication:
                        with self.database.sessions() as database:
                            candidate = Repository(database).document(user_id, document_id, lock=True)
                            self.publication.assert_build(database, candidate)
                            guard()
                            database.commit()
                    self.vectors.delete_document(user_id, document_id)
                    guard()
                    path = self.file_path(snapshot[0])
                    if self.settings.document_versions_enabled or self.queue or self.settings.hierarchy_enabled:
                        # Verify immutable source bytes before parsing or using the normalized cache.
                        with path.open("rb") as source:
                            actual_hash = hashlib.file_digest(source, "sha256").hexdigest()
                        if actual_hash != snapshot[4]:
                            raise AppError("DOCUMENT_SOURCE_CHANGED", "原始文件与入库版本哈希不符，请上传新版本", 409)
                    if snapshot[3].get("provider") == "mineru":
                        if self.mineru is None:
                            raise AppError("MINERU_NOT_CONFIGURED", "MinerU 解析器尚未配置", 503)
                        loaded = self._load_cache(user_id, document_id, snapshot[3]["source_hash"], snapshot[3]["document_type"])
                        if loaded is None:
                            loaded = self.mineru.parse(
                                path, snapshot[1], snapshot[3]["attempt_id"],
                                lambda batch_id: self._set_batch(user_id, document_id, generation, batch_id, guard=guard),
                                lambda: self._set_phase(user_id, document_id, generation, "parsing", guard=guard),
                                batch_id=snapshot[3].get("batch_id"),
                            )
                            guard()
                            self._save_cache(user_id, document_id, snapshot[3]["source_hash"], snapshot[3]["document_type"], loaded)
                    else:
                        loaded = self.loader.load(path, snapshot[1])
                    self._set_phase(user_id, document_id, generation, "embedding", len(loaded), guard=guard)
                    tree = HierarchyBuilder(self.settings).build(loaded, user_id, document_id, snapshot[2]) if self.settings.hierarchy_enabled else None
                    chunks = tree.leaves if tree else self.loader.chunks(loaded, user_id, document_id, snapshot[2])
                    if not chunks:
                        raise AppError("EMPTY_DOCUMENT", "文档没有可入库的文本")
                    self._set_progress(user_id, document_id, generation, 0, len(chunks), guard=guard)
                    for offset in range(0, len(chunks), self.settings.embedding_batch_size):
                        batch = chunks[offset:offset + self.settings.embedding_batch_size]
                        guard()
                        embeddings = self.embeddings.embed_documents([chunk.content for chunk in batch])
                        guard()
                        self.vectors.upsert(batch, embeddings)
                        guard()
                        self._set_progress(user_id, document_id, generation, offset + len(batch), len(chunks), guard=guard)
                    leaf_sha256 = None
                    if self.publication:
                        self._set_phase(user_id, document_id, generation, "verifying", len(loaded), guard=guard)
                        leaf_sha256 = self.publication.verify(document, chunks)
                        guard()
                    with self.database.sessions() as database:
                        document = Repository(database).document(user_id, document_id, lock=True)
                        metadata = dict(document.parser_metadata or {})
                        if int(metadata.get("generation", 0)) != generation or document.status != "processing":
                            return
                        metadata.update(phase="done", normalized_schema_version=1, parsed_unit_count=len(loaded), failed_phase=None, error_code=None)
                        document.parser_metadata = metadata
                        document.status = "ready"
                        document.chunk_count = len(chunks)
                        document.error_message = None
                        if self.publication:
                            self.publication.verified(database, document, len(chunks), leaf_sha256)
                        if tree:
                            publish_tree(database, document, tree)
                        if self.queue:
                            self.queue.progress(database, document_id, "done", processed_units=len(chunks), total_units=len(chunks))
                        guard()
                        database.commit()
                except Exception as error:
                    guard()
                    self._fail(user_id, document_id, generation, error, guard=guard)
                    if raise_errors:
                        raise
        except Exception as error:
            logger.error("ingestion_transaction_failed", extra={"resource_id": str(document_id), "error_type": type(error).__name__})
            if raise_errors:
                raise

    def _set_batch(self, user_id: UUID, document_id: UUID, generation: int, batch_id: str, *, guard=lambda: None) -> None:
        guard()
        with self.database.sessions() as database:
            document = Repository(database).document(user_id, document_id, lock=True)
            metadata = dict(document.parser_metadata or {})
            if int(metadata.get("generation", 0)) != generation or document.status != "processing":
                return
            metadata.update(batch_id=batch_id, phase="uploading")
            document.parser_metadata = metadata
            if self.queue:
                self.queue.progress(database, document_id, "uploading")
            guard()
            database.commit()

    def _set_phase(self, user_id: UUID, document_id: UUID, generation: int, phase: str, parsed_units: int | None = None, *, guard=lambda: None) -> None:
        guard()
        with self.database.sessions() as database:
            document = Repository(database).document(user_id, document_id, lock=True)
            metadata = dict(document.parser_metadata or {})
            if int(metadata.get("generation", 0)) != generation or document.status != "processing":
                return
            metadata.update(phase=phase, parsed_unit_count=parsed_units)
            document.parser_metadata = metadata
            if self.queue:
                self.queue.progress(database, document_id, phase)
            guard()
            database.commit()

    def _set_progress(self, user_id, document_id, generation, processed, total, *, guard):
        if not self.queue:
            return
        guard()
        with self.database.sessions() as database:
            document = Repository(database).document(user_id, document_id, lock=True)
            if document.status != "processing" or int((document.parser_metadata or {}).get("generation", 0)) != generation:
                return
            self.queue.progress(database, document_id, "indexing", processed_units=processed, total_units=total)
            guard()
            database.commit()

    def _fail(self, user_id: UUID, document_id: UUID, generation: int, error: Exception, *, guard=lambda: None) -> None:
        guard()
        with self.database.sessions() as database:
            document = Repository(database).document(user_id, document_id, lock=True)
            metadata = dict(document.parser_metadata or {})
            if int(metadata.get("generation", 0)) != generation or document.status != "processing":
                return
        try:
            guard()
            cleanup_vectors = self.vectors
            if self.publication:
                with self.database.sessions() as database:
                    candidate = Repository(database).document(user_id, document_id)
                    index = self.publication.index(database, candidate)
                    if index.state in {"published", "superseded", "retiring", "cleaned"}:
                        raise AppError("DOCUMENT_INDEX_IMMUTABLE", "不能清理已发布索引", 409)
                    if index.collection_name != self.settings.milvus_collection:
                        cleanup_vectors = self.vectors.for_collection(index.collection_name)
            cleanup_vectors.delete_document(user_id, document_id)
        except Exception as cleanup_error:
            logger.warning("ingestion_cleanup_failed", extra={"resource_id": str(document_id), "error_type": type(cleanup_error).__name__})
        with self.database.sessions() as database:
            guard()
            document = Repository(database).document(user_id, document_id, lock=True)
            metadata = dict(document.parser_metadata or {})
            if int(metadata.get("generation", 0)) != generation or document.status != "processing":
                return
            code = error.code if isinstance(error, AppError) else "INGESTION_FAILED"
            metadata.update(failed_phase=metadata.get("phase"), phase="failed", error_code=code)
            if self.queue:
                metadata["retryable"] = self.queue.retryable(error)
            document.parser_metadata = metadata
            document.status = "failed"
            document.chunk_count = 0
            document.error_message = error.message if isinstance(error, AppError) else "文档处理失败，请检查服务状态后重试"
            if self.publication:
                index = database.get(DocumentIndex, document_id)
                if index is not None and index.state in {"building", "verified", "failed"}:
                    index.state = "failed"
            guard()
            database.commit()
        logger.warning("ingestion_failed", extra={"resource_id": str(document_id), "error_type": type(error).__name__})

    def delete(self, user_id: UUID, document_id: UUID, *, organization_id=None):
        if self.publication:
            return self.tasks.retire(user_id, document_id, organization_id=organization_id)
        with self.exclusive(document_id), self.database.sessions() as database:
            repository = Repository(database)
            document = self.workspace_document(repository, user_id, document_id, organization_id, lock=True)
            from sqlalchemy import inspect
            if inspect(database.connection()).has_table("document_indexes", schema="rag") and database.get(DocumentIndex, document_id):
                raise AppError("DOCUMENT_PUBLICATION_DISABLED", "请启用文档发布流程后退休此版本", 409)
            if self.settings.document_versions_enabled:
                repository.prepare_version_delete(user_id, document_id)
            document.status = "deleting"
            database.commit()
            document = repository.document(user_id, document_id, lock=True)
            try:
                self.vectors.delete_document(user_id, document_id)
                self.file_path(document.file_path).unlink(missing_ok=True)
                parsed = (self.settings.parsed_dir / str(user_id) / str(document_id)).resolve()
                root = self.settings.parsed_dir.resolve()
                if parsed.is_relative_to(root) and parsed != root and parsed.is_dir():
                    for child in parsed.iterdir():
                        if child.is_file():
                            child.unlink(missing_ok=True)
                    parsed.rmdir()
            except Exception as error:
                document.error_message = error.message if isinstance(error, AppError) else "删除失败，请重试 DELETE 请求"
                database.commit()
                raise
            if self.settings.document_versions_enabled:
                repository.finish_version_delete(document_id)
            database.delete(document)
            database.commit()
