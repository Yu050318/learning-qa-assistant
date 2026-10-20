"""PostgreSQL queue; document changes and enqueue share the caller's transaction."""
from dataclasses import dataclass
from datetime import timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select

from app.core.errors import AppError, UpstreamError
from app.infrastructure.persistence.models import DocumentRecord, DocumentTask, IngestionJob
from app.infrastructure.persistence.repository import Repository


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def lease_lost():
    return AppError("INGESTION_LEASE_LOST", "入库任务租约已失效，等待 Worker 接管", 409)


@dataclass(frozen=True)
class Claim:
    document_id: UUID
    user_id: UUID
    token: UUID
    exhausted: bool
    kind: str = "ingest"


class IngestionQueue:
    def __init__(self, settings, database):
        self.settings = settings
        self.database = database

    def now(self, session):
        clock = func.clock_timestamp() if session.get_bind().dialect.name == "postgresql" else func.current_timestamp()
        return aware(session.scalar(select(clock)))

    def enqueue(self, session, document, *, reset=False, kind="ingest", details=None, parent_task_id=None):
        job = session.get(IngestionJob, document.id)
        if job is None:
            job = IngestionJob(document_id=document.id, user_id=document.user_id)
        elif not reset:
            return
        if job.task_id:
            previous = session.get(DocumentTask, job.task_id)
            if previous is not None and previous.state in {"queued", "running"}:
                previous.state, previous.phase = "cancelled", "cancelled"
                previous.error_code = "SUPERSEDED_BY_RETIREMENT" if kind == "delete" else "SUPERSEDED_BY_RETRY"
                previous.updated_at = self.now(session)
        task = DocumentTask(id=uuid4(), document_id=document.id, user_id=document.user_id,
            parent_task_id=parent_task_id,
            organization_id=document.organization_id,
            original_name=document.original_name, kind=kind, state="queued", phase="queued",
            max_attempts=self.settings.ingestion_max_attempts, details=details or {})
        session.add(task)
        session.flush()
        session.add(job)
        job.task_id = task.id
        job.state = "queued"
        job.kind = kind
        job.attempts = 0
        job.max_attempts = self.settings.ingestion_max_attempts
        job.available_at = self.now(session)
        job.claim_token = job.lease_expires_at = job.last_error_code = None
        self.sync_parent(session, parent_task_id)
        return task

    def sync_parent(self, session, parent_id):
        if parent_id is None:
            return
        parent = session.get(DocumentTask, parent_id, with_for_update=True)
        if parent is None or parent.kind != "delete_series":
            raise AppError("INGESTION_TASK_DIVERGED", "整组退休任务历史不一致", 409)
        # Follow the current receipt for each version, so failed retry receipts
        # remain immutable without counting the same version twice.
        children = session.scalars(select(DocumentTask).join(IngestionJob, IngestionJob.task_id == DocumentTask.id).where(
            DocumentTask.parent_task_id == parent_id)).all()
        parent.processed_units = parent.details.get("already_retired", 0) + sum(child.state == "succeeded" for child in children)
        parent.updated_at = self.now(session)
        if parent.processed_units == parent.total_units:
            parent.state, parent.phase, parent.error_code = "succeeded", "done", None
        elif any(child.state in {"failed", "cancelled"} for child in children):
            parent.state, parent.phase, parent.error_code = "failed", "failed", "DOCUMENT_SERIES_CLEANUP_INCOMPLETE"
        elif any(child.phase == "waiting_runs" for child in children):
            parent.state, parent.phase, parent.error_code = "queued", "waiting_runs", None
        else:
            parent.state, parent.phase, parent.error_code = "running", "cleaning_versions", None

    def sync_task(self, session, job, *, phase=None, processed_units=None, total_units=None):
        if job.task_id is None:
            return
        task = session.get(DocumentTask, job.task_id)
        if task is None:
            raise AppError("INGESTION_TASK_DIVERGED", "任务历史与执行队列不一致", 409)
        if (task.document_id, task.user_id, task.kind) != (job.document_id, job.user_id, job.kind):
            raise AppError("INGESTION_TASK_DIVERGED", "任务历史的归属或种类与执行队列不一致", 409)
        task.state, task.attempts, task.max_attempts = job.state, job.attempts, job.max_attempts
        task.error_code, task.updated_at = job.last_error_code, self.now(session)
        if phase is not None:
            task.phase = phase
        if processed_units is not None:
            task.processed_units = processed_units
        if total_units is not None:
            task.total_units = total_units
        self.sync_parent(session, task.parent_task_id)

    def progress(self, session, document_id, phase, *, processed_units=None, total_units=None):
        job = session.get(IngestionJob, document_id)
        if job is not None:
            self.sync_task(session, job, phase=phase, processed_units=processed_units, total_units=total_units)

    def reconcile(self, limit=100):
        with self.database.sessions() as session:
            documents = session.scalars(
                select(DocumentRecord).outerjoin(IngestionJob, IngestionJob.document_id == DocumentRecord.id)
                .where(DocumentRecord.status == "processing", IngestionJob.document_id.is_(None))
                .order_by(DocumentRecord.created_at).limit(limit)
                .with_for_update(of=DocumentRecord, skip_locked=True)
            ).all()
            for document in documents:
                self.enqueue(session, document)
            session.commit()
            return len(documents)

    def claim(self):
        with self.database.sessions() as session:
            now = self.now(session)
            job = session.scalar(select(IngestionJob).where(or_(
                and_(IngestionJob.state == "queued", IngestionJob.available_at <= now),
                and_(IngestionJob.state == "running", IngestionJob.lease_expires_at <= now),
            )).order_by(IngestionJob.available_at, IngestionJob.document_id)
                .limit(1).with_for_update(skip_locked=True))
            if job is None:
                return None
            exhausted = job.attempts >= job.max_attempts
            if not exhausted:
                job.attempts += 1
            job.state = "running"
            job.claim_token = uuid4()
            job.lease_expires_at = now + timedelta(seconds=self.settings.ingestion_lease_seconds)
            result = Claim(job.document_id, job.user_id, job.claim_token, exhausted, job.kind)
            receipt = session.get(DocumentTask, job.task_id) if job.task_id else None
            phase = "waiting_runs" if receipt and job.kind == "delete" and receipt.phase == "waiting_runs" else "running"
            self.sync_task(session, job, phase=phase)
            session.commit()
            return result

    def owned(self, session, claim, *, lock=False):
        query = select(IngestionJob).where(IngestionJob.document_id == claim.document_id)
        if lock:
            query = query.with_for_update()
        job = session.scalar(query)
        if (job is None or job.state != "running" or job.claim_token != claim.token
                or job.lease_expires_at is None or aware(job.lease_expires_at) <= self.now(session)):
            raise lease_lost()
        return job

    def assert_owned(self, claim):
        with self.database.sessions() as session:
            self.owned(session, claim)

    def heartbeat(self, claim):
        with self.database.sessions() as session:
            job = self.owned(session, claim, lock=True)
            job.lease_expires_at = self.now(session) + timedelta(seconds=self.settings.ingestion_lease_seconds)
            session.commit()

    def release_busy(self, claim):
        with self.database.sessions() as session:
            job = self.owned(session, claim, lock=True)
            if not claim.exhausted:
                job.attempts -= 1
            job.state = "queued"
            job.available_at = self.now(session) + timedelta(seconds=self.settings.ingestion_poll_seconds)
            if aware(job.lease_expires_at) <= self.now(session):
                raise lease_lost()
            job.claim_token = job.lease_expires_at = None
            self.sync_task(session, job, phase="queued")
            session.commit()

    def finish(self, claim, error=None):
        """Called while holding the per-document advisory lock, including after crashes."""
        from app.application.ingestion import prepare_retry_metadata

        with self.database.sessions() as session:
            document = Repository(session).document(claim.user_id, claim.document_id, lock=True)
            job = self.owned(session, claim, lock=True)
            code = error.code if isinstance(error, AppError) else "INGESTION_FAILED"
            phase = None
            if claim.kind == "delete":
                if document.status == "retired":
                    job.state, job.last_error_code, phase = "succeeded", None, "done"
                elif isinstance(error, AppError) and error.code == "DOCUMENT_INDEX_PINNED":
                    job.state, job.last_error_code, phase = "queued", None, "waiting_runs"
                    job.attempts = max(0, job.attempts - 1)
                    job.available_at = self.now(session) + timedelta(seconds=5)
                elif error is not None and self.retryable(error) and job.attempts < job.max_attempts:
                    job.state, job.last_error_code, phase = "queued", code, "retry_wait"
                    job.available_at = self.now(session) + timedelta(seconds=min(300, 5 * 2 ** (job.attempts - 1)))
                else:
                    job.state, phase = "failed", "failed"
                    job.last_error_code = "INGESTION_ATTEMPTS_EXHAUSTED" if claim.exhausted else code
                document.error_message = None if job.state == "succeeded" else "等待相关运行结束" if phase == "waiting_runs" else "清理尚未完成，可在任务面板重试"
            elif claim.kind == "hierarchy":
                from app.infrastructure.loaders.hierarchy import HierarchyBuilder
                manifest = (document.parser_metadata or {}).get("hierarchy", {})
                valid = (manifest.get("fingerprint") == HierarchyBuilder(self.settings).fingerprint
                         and manifest.get("source_hash") == document.content_hash
                         and manifest.get("leaf_count") == document.chunk_count
                         and manifest.get("generation") == (document.parser_metadata or {}).get("generation", 0))
                if document.status == "deleting":
                    job.state = "cancelled"
                elif document.status == "ready" and valid:
                    job.state, job.last_error_code = "succeeded", None
                elif error is not None and self.retryable(error) and job.attempts < job.max_attempts:
                    job.state, job.last_error_code = "queued", code
                    job.available_at = self.now(session) + timedelta(seconds=min(300, 5 * 2 ** (job.attempts - 1)))
                else:
                    job.state = "failed"
                    job.last_error_code = "INGESTION_ATTEMPTS_EXHAUSTED" if claim.exhausted else code
                # Backfill failures never invalidate the original document or vectors.
            elif document.status == "ready":
                job.state = "succeeded"
                job.last_error_code = None
            elif document.status == "deleting":
                job.state = "cancelled"
            elif error is not None and self.retryable(error) and job.attempts < job.max_attempts:
                job.state = "queued"
                job.last_error_code = code
                job.available_at = self.now(session) + timedelta(seconds=min(300, 5 * 2 ** (job.attempts - 1)))
                document.status = "processing"
                document.error_message = "暂时无法完成入库，等待 Worker 重试"
                metadata = prepare_retry_metadata(document.parser_metadata or {})
                metadata.update(phase="retry_wait", error_code=code)
                document.parser_metadata = metadata
                document.chunk_count = 0
            else:
                job.state = "failed"
                job.last_error_code = "INGESTION_ATTEMPTS_EXHAUSTED" if claim.exhausted else code
                document.status = "failed"
                document.chunk_count = 0
                document.error_message = "入库任务失败，请检查服务后手动重试"
                metadata = dict(document.parser_metadata or {})
                metadata.update(phase="failed", error_code=job.last_error_code)
                document.parser_metadata = metadata
            if aware(job.lease_expires_at) <= self.now(session):
                raise lease_lost()
            job.claim_token = job.lease_expires_at = None
            self.sync_task(session, job, phase=phase or ("done" if job.state == "succeeded" else "retry_wait" if job.state == "queued" else job.state))
            session.commit()

    @staticmethod
    def retryable(error):
        return (isinstance(error, UpstreamError) and error.retryable) or (
            isinstance(error, AppError) and error.code in {
                "MINERU_RATE_LIMITED", "MINERU_UPSTREAM_UNAVAILABLE", "MINERU_PARSE_TIMEOUT",
                "MINERU_DOWNLOAD_TIMEOUT", "MINERU_DOWNLOAD_FAILED", "MILVUS_UNAVAILABLE",
            }
        )

    def status(self, user_id, document_id):
        with self.database.sessions() as session:
            Repository(session).document(user_id, document_id)
            job = session.scalar(select(IngestionJob).where(
                IngestionJob.document_id == document_id, IngestionJob.user_id == user_id))
            if job is None:
                return None
            return {"kind": job.kind, "state": job.state, "attempts": job.attempts, "max_attempts": job.max_attempts,
                    "available_at": job.available_at, "last_error_code": job.last_error_code}
