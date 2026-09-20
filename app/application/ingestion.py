import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import BinaryIO
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from langchain_core.documents import Document

from app.core.config import Settings
from app.core.errors import AppError
from app.domain.contracts import EmbeddingProvider, VectorStore
from app.infrastructure.loaders.local import LocalDocumentLoader, validate_document_container, validate_upload
from app.infrastructure.loaders.mineru import DOCUMENT_TYPES, MinerUParser
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import DocumentRecord
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
    return result


class IngestionService:
    def __init__(self, settings: Settings, database: Database, loader: LocalDocumentLoader, mineru: MinerUParser | None, embeddings: EmbeddingProvider, vectors: VectorStore):
        self.settings = settings
        self.database = database
        self.loader = loader
        self.mineru = mineru
        self.embeddings = embeddings
        self.vectors = vectors
        self.locks = [Lock() for _ in range(64)]
        self.executor = ThreadPoolExecutor(max_workers=settings.ingestion_workers, thread_name_prefix="ingestion")
        self.scheduled: set[UUID] = set()
        self.schedule_lock = Lock()

    def schedule(self, user_id: UUID, document_id: UUID) -> bool:
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
        with self.database.sessions() as database:
            documents = Repository(database).processing_documents()
            for document in documents:
                metadata = document.parser_metadata or {}
                if metadata.get("phase") == "uploading":
                    document.parser_metadata = prepare_retry_metadata(metadata)
            database.commit()
        return sum(self.schedule(document.user_id, document.id) for document in documents)

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)

    @contextmanager
    def exclusive(self, document_id: UUID):
        lock = self.locks[document_id.int % len(self.locks)]
        if not lock.acquire(blocking=False):
            raise AppError("RESOURCE_BUSY", "文档正在处理中，请稍后重试", 409)
        try:
            yield
        finally:
            lock.release()

    def file_path(self, stored_path: str) -> Path:
        root = self.settings.upload_dir.resolve()
        path = Path(stored_path).resolve()
        if not path.is_relative_to(root) or path == root:
            raise AppError("INVALID_STORAGE_PATH", "文件路径不在受控目录中", 500)
        return path

    def _cache_path(self, document_id: UUID) -> Path:
        root = self.settings.parsed_dir.resolve()
        path = (root / str(document_id) / "normalized.json").resolve()
        if not path.is_relative_to(root) or path == root:
            raise AppError("INVALID_STORAGE_PATH", "解析缓存路径不在受控目录中", 500)
        return path

    def _save_cache(self, document_id: UUID, source_hash: str, document_type: str, documents: list[Document]) -> None:
        path = self._cache_path(document_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        payload = {
            "schema_version": 1, "source_hash": source_hash, "document_type": document_type,
            "documents": [{"page_content": item.page_content, "metadata": item.metadata} for item in documents],
        }
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def _load_cache(self, document_id: UUID, source_hash: str, document_type: str) -> list[Document] | None:
        path = self._cache_path(document_id)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != 1 or payload.get("source_hash") != source_hash or payload.get("document_type") != document_type:
                return None
            return [Document(page_content=item["page_content"], metadata=item.get("metadata") or {}) for item in payload["documents"]]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def upload(self, user_id: UUID, filename: str, content_type: str | None, stream: BinaryIO) -> DocumentRecord:
        name, extension = validate_upload(filename, content_type)
        if extension in DOCUMENT_TYPES and (not self.settings.mineru_enabled or not self.settings.mineru_api_token.get_secret_value()):
            raise AppError("MINERU_NOT_CONFIGURED", "PDF/Office 解析需要启用并配置 MinerU", 503)
        self.settings.upload_dir.mkdir(parents=True, exist_ok=True)
        document_id = uuid4()
        path = self.file_path(str(self.settings.upload_dir / f"{document_id}{extension}"))
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
                if repository.duplicate(user_id, digest.hexdigest()):
                    raise AppError("DUPLICATE_DOCUMENT", "相同内容的文档已存在，请查询状态或重试", 409)
                document = DocumentRecord(
                    id=document_id, user_id=user_id, original_name=name, file_type=extension,
                    file_path=str(path), content_hash=digest.hexdigest(), size_bytes=size, status="processing",
                    embedding_model=self.settings.embedding_model,
                    parser_metadata={
                        "provider": "mineru" if extension in DOCUMENT_TYPES else "local",
                        "document_type": DOCUMENT_TYPES.get(extension, "text"), "phase": "validating",
                        "generation": 1, "attempt_id": str(uuid4()), "batch_id": None,
                        "model_version": self.settings.mineru_model_version if extension in DOCUMENT_TYPES else None,
                        "source_hash": digest.hexdigest(), "normalized_schema_version": None,
                        "parsed_unit_count": None, "unit_kind": None, "failed_phase": None, "error_code": None,
                    },
                )
                database.add(document)
                try:
                    database.commit()
                except IntegrityError:
                    database.rollback()
                    if repository.duplicate(user_id, digest.hexdigest()):
                        raise AppError("DUPLICATE_DOCUMENT", "相同内容的文档已存在", 409) from None
                    raise
                saved = True
                return document
        finally:
            if not saved:
                path.unlink(missing_ok=True)

    def list(self, user_id: UUID, offset: int, limit: int):
        with self.database.sessions() as database:
            return Repository(database).documents(user_id, offset, limit)

    def search(self, user_id: UUID, query: str, status: str | None, offset: int, limit: int):
        with self.database.sessions() as database:
            return Repository(database).search_documents(user_id, query, status, offset, limit)

    def get(self, user_id: UUID, document_id: UUID):
        with self.database.sessions() as database:
            return Repository(database).document(user_id, document_id)

    def retry(self, user_id: UUID, document_id: UUID):
        with self.exclusive(document_id):
            with self.database.sessions() as database:
                document = Repository(database).document(user_id, document_id, lock=True)
                if document.status not in {"failed", "processing"}:
                    raise AppError("INVALID_DOCUMENT_STATE", "只能重试失败或中断的文档", 409)
                if not self.file_path(document.file_path).is_file():
                    raise AppError("SOURCE_FILE_MISSING", "原始文件不存在，请删除记录后重新上传", 409)
                metadata = prepare_retry_metadata(document.parser_metadata or {})
                document.parser_metadata = metadata
                document.status = "processing"
                document.error_message = None
                document.embedding_model = self.settings.embedding_model
                document.chunk_count = 0
                database.commit()
                return document

    def process(self, user_id: UUID, document_id: UUID) -> None:
        try:
            with self.exclusive(document_id):
                with self.database.sessions() as database:
                    document = Repository(database).document(user_id, document_id, lock=True)
                    if document.status != "processing":
                        return
                    metadata = dict(document.parser_metadata or {})
                    generation = int(metadata.get("generation", 0))
                    metadata["phase"] = "parsing" if metadata.get("provider") == "mineru" else "normalizing"
                    document.parser_metadata = metadata
                    snapshot = (document.file_path, document.file_type, document.original_name, metadata)
                    database.commit()
                try:
                    self.vectors.delete_document(user_id, document_id)
                    path = self.file_path(snapshot[0])
                    if snapshot[3].get("provider") == "mineru":
                        if self.mineru is None:
                            raise AppError("MINERU_NOT_CONFIGURED", "MinerU 解析器尚未配置", 503)
                        loaded = self._load_cache(document_id, snapshot[3]["source_hash"], snapshot[3]["document_type"])
                        if loaded is None:
                            loaded = self.mineru.parse(
                                path, snapshot[1], snapshot[3]["attempt_id"],
                                lambda batch_id: self._set_batch(user_id, document_id, generation, batch_id),
                                lambda: self._set_phase(user_id, document_id, generation, "parsing"),
                                batch_id=snapshot[3].get("batch_id"),
                            )
                            self._save_cache(document_id, snapshot[3]["source_hash"], snapshot[3]["document_type"], loaded)
                    else:
                        loaded = self.loader.load(path, snapshot[1])
                    self._set_phase(user_id, document_id, generation, "embedding", len(loaded))
                    chunks = self.loader.chunks(loaded, user_id, document_id, snapshot[2])
                    if not chunks:
                        raise AppError("EMPTY_DOCUMENT", "文档没有可入库的文本")
                    for offset in range(0, len(chunks), self.settings.embedding_batch_size):
                        batch = chunks[offset:offset + self.settings.embedding_batch_size]
                        embeddings = self.embeddings.embed_documents([chunk.content for chunk in batch])
                        self.vectors.upsert(batch, embeddings)
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
                        database.commit()
                except Exception as error:
                    self._fail(user_id, document_id, generation, error)
        except Exception as error:
            logger.error("ingestion_transaction_failed", extra={"resource_id": str(document_id), "error_type": type(error).__name__})

    def _set_batch(self, user_id: UUID, document_id: UUID, generation: int, batch_id: str) -> None:
        with self.database.sessions() as database:
            document = Repository(database).document(user_id, document_id, lock=True)
            metadata = dict(document.parser_metadata or {})
            if int(metadata.get("generation", 0)) != generation or document.status != "processing":
                return
            metadata.update(batch_id=batch_id, phase="uploading")
            document.parser_metadata = metadata
            database.commit()

    def _set_phase(self, user_id: UUID, document_id: UUID, generation: int, phase: str, parsed_units: int | None = None) -> None:
        with self.database.sessions() as database:
            document = Repository(database).document(user_id, document_id, lock=True)
            metadata = dict(document.parser_metadata or {})
            if int(metadata.get("generation", 0)) != generation or document.status != "processing":
                return
            metadata.update(phase=phase, parsed_unit_count=parsed_units)
            document.parser_metadata = metadata
            database.commit()

    def _fail(self, user_id: UUID, document_id: UUID, generation: int, error: Exception) -> None:
        with self.database.sessions() as database:
            document = Repository(database).document(user_id, document_id, lock=True)
            metadata = dict(document.parser_metadata or {})
            if int(metadata.get("generation", 0)) != generation or document.status != "processing":
                return
        try:
            self.vectors.delete_document(user_id, document_id)
        except Exception as cleanup_error:
            logger.warning("ingestion_cleanup_failed", extra={"resource_id": str(document_id), "error_type": type(cleanup_error).__name__})
        with self.database.sessions() as database:
            document = Repository(database).document(user_id, document_id, lock=True)
            metadata = dict(document.parser_metadata or {})
            if int(metadata.get("generation", 0)) != generation or document.status != "processing":
                return
            code = error.code if isinstance(error, AppError) else "INGESTION_FAILED"
            metadata.update(failed_phase=metadata.get("phase"), phase="failed", error_code=code)
            document.parser_metadata = metadata
            document.status = "failed"
            document.chunk_count = 0
            document.error_message = error.message if isinstance(error, AppError) else "文档处理失败，请检查服务状态后重试"
            database.commit()
        logger.warning("ingestion_failed", extra={"resource_id": str(document_id), "error_type": type(error).__name__})

    def delete(self, user_id: UUID, document_id: UUID) -> None:
        with self.exclusive(document_id), self.database.sessions() as database:
            repository = Repository(database)
            document = repository.document(user_id, document_id, lock=True)
            document.status = "deleting"
            database.commit()
            document = repository.document(user_id, document_id, lock=True)
            try:
                self.vectors.delete_document(user_id, document_id)
                self.file_path(document.file_path).unlink(missing_ok=True)
                parsed = (self.settings.parsed_dir / str(document_id)).resolve()
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
            database.delete(document)
            database.commit()
