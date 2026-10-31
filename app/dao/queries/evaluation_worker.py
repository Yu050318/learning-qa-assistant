"""Persistence queries for app/manager/evaluation_worker.py."""
from sqlalchemy import func, select
from app.entity.do.models import ChatMessage, ChatRunEvent, ChatSession, EvaluationCaseResult, ModelCall
from app.dao.postgres.transactions import unwrap


def latest_run_error(run, session):
    session = unwrap(session)
    return (session.scalar(select(ChatRunEvent).where(ChatRunEvent.run_id == run.id, ChatRunEvent.event == "error")
                    .order_by(ChatRunEvent.sequence.desc()).limit(1)))


def finished_case_count(job, session):
    session = unwrap(session)
    return (session.scalar(select(func.count()).select_from(EvaluationCaseResult).where(EvaluationCaseResult.job_id == job.id,
                EvaluationCaseResult.state.in_(("succeeded", "failed", "cancelled")))))


def ordered_cases(job, session):
    session = unwrap(session)
    return (session.scalars(select(EvaluationCaseResult).where(EvaluationCaseResult.job_id == job.id).order_by(EvaluationCaseResult.position)).all())


def case_model_calls(cases, session):
    session = unwrap(session)
    return (session.scalars(select(ModelCall).where(ModelCall.session_id.in_([case.session_id for case in cases if case.session_id]))).all())


def unfinished_case_ids(job, session):
    session = unwrap(session)
    return (session.scalars(select(EvaluationCaseResult.id).where(EvaluationCaseResult.job_id == job.id,
                    EvaluationCaseResult.state.in_(("queued", "running"))).order_by(EvaluationCaseResult.position)).all())
