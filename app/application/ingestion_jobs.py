"""PostgreSQL queue; document changes and enqueue share the caller's transaction."""
from dataclasses import dataclass
from datetime import timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select

from app.core.errors import AppError, UpstreamError
from app.infrastructure.persistence.models import DocumentRecord, IngestionJob
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


class IngestionQueue:
    def __init__(self, settings, database):
        self.settings = settings
        self.database = database

    def now(self, session):
        clock = func.clock_timestamp() if session.get_bind().dialect.name == "postgresql" else func.current_timestamp()
        return aware(session.scalar(select(clock)))

    def enqueue(self, session, document, *, reset=False):
        job = session.get(IngestionJob, document.id)
        if job is None:
            job = IngestionJob(document_id=document.id, user_id=document.user_id)
            session.add(job)
        elif not reset:
            return
        job.state = "queued"
        job.attempts = 0
        job.max_attempts = self.settings.ingestion_max_attempts
        job.available_at = self.now(session)
        job.claim_token = job.lease_expires_at = job.last_error_code = None

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
            result = Claim(job.document_id, job.user_id, job.claim_token, exhausted)
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
            session.commit()

    def finish(self, claim, error=None):
        """Called while holding the per-document advisory lock, including after crashes."""
        from app.application.ingestion import prepare_retry_metadata

        with self.database.sessions() as session:
            document = Repository(session).document(claim.user_id, claim.document_id, lock=True)
            job = self.owned(session, claim, lock=True)
            code = error.code if isinstance(error, AppError) else "INGESTION_FAILED"
            if document.status == "ready":
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
            return {"state": job.state, "attempts": job.attempts, "max_attempts": job.max_attempts,
                    "available_at": job.available_at, "last_error_code": job.last_error_code}
