"""Persistence queries for app/service/evaluation_jobs.py."""
from sqlalchemy import and_, func, or_, select
from app.entity.do.models import ChatRun, DocumentRecord, EvaluationCaseResult, EvaluationDataset, EvaluationJob, User
from app.dao.postgres.transactions import unwrap


def get_user(lock, session, uid):
    session = unwrap(session)
    query = select(User).where(User.id == uid)
    return session.scalar(query.with_for_update() if lock else query)


def find_dataset_fingerprint(dataset, session, uid):
    session = unwrap(session)
    return (session.scalar(select(EvaluationDataset).where(
                EvaluationDataset.created_by == uid, EvaluationDataset.fingerprint == dataset.fingerprint)))


def get_dataset(dataset_id, session, uid):
    session = unwrap(session)
    return (session.scalar(select(EvaluationDataset).where(EvaluationDataset.id == dataset_id, EvaluationDataset.created_by == uid)))


def list_datasets(limit, offset, session, uid):
    session = unwrap(session)
    return (session.scalars(select(EvaluationDataset).where(EvaluationDataset.created_by == uid)
                .order_by(EvaluationDataset.created_at.desc(), EvaluationDataset.id).offset(offset).limit(limit)).all())


def get_job(job_id, lock, session, uid):
    session = unwrap(session)
    query = select(EvaluationJob).where(EvaluationJob.id == job_id, EvaluationJob.created_by == uid)
    return session.scalar(query.with_for_update() if lock else query)


def page_jobs(limit, offset, session, uid, state=None):
    query = select(EvaluationJob).where(EvaluationJob.created_by == uid)
    if state is not None:
        query = query.where(EvaluationJob.state == state)
    return unwrap(session).scalars(query.order_by(EvaluationJob.created_at.desc(), EvaluationJob.id).offset(offset).limit(limit)).all()


def list_cases(job_id, limit, offset, session):
    session = unwrap(session)
    return (session.scalars(select(EvaluationCaseResult).where(EvaluationCaseResult.job_id == job_id)
                .order_by(EvaluationCaseResult.position).offset(offset).limit(limit)).all())


def find_idempotent_job(client_key, session, uid):
    session = unwrap(session)
    return (session.scalar(select(EvaluationJob).where(EvaluationJob.created_by == uid, EvaluationJob.client_key == client_key)))


def count_active_jobs(ACTIVE, session, uid):
    session = unwrap(session)
    return (session.scalar(select(func.count()).select_from(EvaluationJob).where(EvaluationJob.created_by == uid, EvaluationJob.state.in_(ACTIVE))))


def ordered_documents(owners, session):
    session = unwrap(session)
    return (session.scalars(select(DocumentRecord).where(DocumentRecord.id.in_(owners)).order_by(DocumentRecord.id)).all())


def scope_documents(ids, session):
    session = unwrap(session)
    return (session.scalars(select(DocumentRecord).where(DocumentRecord.id.in_(ids))).all())


def unfinished_cases(row, session):
    session = unwrap(session)
    return (session.scalars(select(EvaluationCaseResult).where(EvaluationCaseResult.job_id == row.id,
            EvaluationCaseResult.state.in_(("queued", "running")))).all())


def abandoned_runs(TERMINAL, session):
    session = unwrap(session)
    return (session.execute(select(EvaluationJob.created_by, ChatRun.id).join(EvaluationCaseResult,
                EvaluationCaseResult.job_id == EvaluationJob.id).join(ChatRun, ChatRun.id == EvaluationCaseResult.run_id)
                .where(EvaluationJob.state.in_(TERMINAL), ChatRun.execution_kind == "evaluation", ChatRun.state.in_(("queued", "running", "waiting", "cancelling"))).limit(10)).all())


def lock_claimable_jobs(now, session):
    query = select(EvaluationJob).where(or_(and_(EvaluationJob.state == 'queued', EvaluationJob.available_at <= now), and_(EvaluationJob.state.in_(('running', 'cancelling')), EvaluationJob.lease_expires_at <= now)))
    return list(unwrap(session).scalars(query.order_by(EvaluationJob.created_at, EvaluationJob.id).limit(10).with_for_update(skip_locked=True)))


def case_run_ids(row, session):
    session = unwrap(session)
    return (list(session.scalars(select(EvaluationCaseResult.run_id).where(EvaluationCaseResult.job_id == row.id, EvaluationCaseResult.run_id.is_not(None)))))
