"""Persistence queries for app/service/ingestion_jobs.py."""
from sqlalchemy import and_, func, or_, select
from app.entity.do.models import DocumentRecord, DocumentTask, IngestionJob
from app.dao.postgres.transactions import unwrap


def child_tasks(parent_id, session):
    session = unwrap(session)
    return (session.scalars(select(DocumentTask).join(IngestionJob, IngestionJob.task_id == DocumentTask.id).where(
            DocumentTask.parent_task_id == parent_id)).all())


def lock_unqueued_documents(limit, session):
    session = unwrap(session)
    return (session.scalars(
                select(DocumentRecord).outerjoin(IngestionJob, IngestionJob.document_id == DocumentRecord.id)
                .where(DocumentRecord.status == "processing", IngestionJob.document_id.is_(None))
                .order_by(DocumentRecord.created_at).limit(limit)
                .with_for_update(of=DocumentRecord, skip_locked=True)
            ).all())


def lock_next_job(now, session):
    session = unwrap(session)
    return (session.scalar(select(IngestionJob).where(or_(
                and_(IngestionJob.state == "queued", IngestionJob.available_at <= now),
                and_(IngestionJob.state == "running", IngestionJob.lease_expires_at <= now),
            )).order_by(IngestionJob.available_at, IngestionJob.document_id)
                .limit(1).with_for_update(skip_locked=True)))


def get_job(claim, session, lock=False):
    session = unwrap(session)
    query = select(IngestionJob).where(IngestionJob.document_id == claim.document_id)
    return session.scalar(query.with_for_update() if lock else query)


def get_owned_status(document_id, session, user_id):
    session = unwrap(session)
    return (session.scalar(select(IngestionJob).where(
                IngestionJob.document_id == document_id, IngestionJob.user_id == user_id)))
