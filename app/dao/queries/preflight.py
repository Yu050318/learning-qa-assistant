"""Persistence queries for app/service/preflight.py."""
from sqlalchemy import func, select, text, inspect, case
from app.entity.do.models import ChatRun, DocumentIndex, DocumentRecord, EvaluationJob, KnowledgeDocument, User
from app.entity.do.models import CapabilityConfigurationVersion
from app.entity.do.models import ChatSession
from app.dao.postgres.transactions import unwrap


def has_table(session, name):
    return inspect(unwrap(session).connection()).has_table(name, schema='rag')


def active_runs(now, session):
    session = unwrap(session)
    return list(session.scalars(select(ChatRun).where(ChatRun.state.in_(("queued", "running", "waiting", "cancelling")),
                ChatRun.expires_at > now)))


def active_evaluations(session):
    session = unwrap(session)
    return list(session.scalars(select(EvaluationJob).where(EvaluationJob.state.in_(("queued", "running", "cancelling")))))


def schema_revision(connection):
    connection = unwrap(connection)
    return (connection.scalar(text("SELECT version_num FROM alembic_version")))


def document_paths(session):
    session = unwrap(session)
    return list(session.scalars(select(DocumentRecord.file_path).where(DocumentRecord.status.not_in(("deleting", "retired")))))


def ready_documents(session):
    session = unwrap(session)
    return (session.scalars(select(DocumentRecord).where(DocumentRecord.status == "ready")).all())


def snapshots(session, name, now):
    if name == 'frozen_runs':
        query = select(ChatRun.execution_snapshot).where(ChatRun.state.in_(('queued', 'running', 'waiting', 'cancelling')), ChatRun.expires_at > now)
    elif name == 'frozen_evaluations':
        query = select(EvaluationJob.scope_snapshot).where(EvaluationJob.state.in_(('queued', 'running', 'cancelling')))
    else:
        raise ValueError('Unknown frozen scope source')
    return list(unwrap(session).scalars(query))


def has_uncleaned_indexes(session):
    session = unwrap(session)
    return (inspect(session.connection()).has_table("document_indexes", schema="rag") and bool(session.scalar(
                    select(DocumentIndex.document_id).where(DocumentIndex.state != "cleaned").limit(1))))


def unselected_shared_series(session):
    session = unwrap(session)
    return (session.execute(select(KnowledgeDocument.knowledge_base_id, KnowledgeDocument.series_id).group_by(
                    KnowledgeDocument.knowledge_base_id, KnowledgeDocument.series_id).having(func.count() > 1,
                    func.sum(case((KnowledgeDocument.is_current.is_(True), 1), else_=0)) == 0).limit(1)).first())


def active_admin_count(session):
    session = unwrap(session)
    return (session.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.is_active.is_(True))))
