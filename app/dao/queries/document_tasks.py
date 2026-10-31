"""Persistence queries for app/service/document_tasks.py."""
from sqlalchemy import delete, select, func
from app.entity.do.models import ChatRun, DocumentIndex, DocumentRecord, DocumentSeries, DocumentVersion, DocumentTask, HierarchyNode, IngestionJob, KnowledgeDocument, RunIndexPin
from app.dao.postgres.transactions import unwrap
from app.entity.query.document_tasks import DocumentTaskQuery


def get_task(session, task_id, uid):
    session = unwrap(session)
    return (session.scalar(select(DocumentTask).where(DocumentTask.id == task_id, DocumentTask.user_id == uid)))


def jobs_by_document(ids, session):
    session = unwrap(session)
    return ({job.document_id: job for job in session.scalars(select(IngestionJob).where(IngestionJob.document_id.in_(ids)))})


def documents_by_id(ids, session):
    session = unwrap(session)
    return ({doc.id: doc for doc in session.scalars(select(DocumentRecord).where(DocumentRecord.id.in_(ids)))})


def indexed_document_ids(ids, session):
    session = unwrap(session)
    return (set(session.scalars(select(DocumentIndex.document_id).where(DocumentIndex.document_id.in_(ids)))))


def page_tasks(session, filters: DocumentTaskQuery):
    session = unwrap(session)
    query = select(DocumentTask).where(DocumentTask.user_id == filters.user_id, DocumentTask.organization_id == filters.organization_id)
    if filters.parent_task_id is not None:
        query = query.where(DocumentTask.parent_task_id == filters.parent_task_id)
    if filters.state:
        query = query.where(DocumentTask.state == filters.state)
    if filters.kind:
        query = query.where(DocumentTask.kind == filters.kind)
    if filters.search:
        escaped = filters.search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        query = query.where(DocumentTask.original_name.ilike(f'%{escaped}%', escape='\\'))
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    rows = session.scalars(query.order_by(DocumentTask.created_at.desc(), DocumentTask.id).offset(filters.offset).limit(filters.limit)).all()
    return total, rows


def remove_shared_bindings(did, session):
    session = unwrap(session)
    return (session.execute(delete(KnowledgeDocument).where(KnowledgeDocument.document_id == did)))


def lock_series(binding, session, uid):
    session = unwrap(session)
    return (session.scalar(select(DocumentSeries).where(DocumentSeries.id == binding.series_id,
                DocumentSeries.user_id == uid).with_for_update(nowait=True)))


def lock_series_documents(series, session):
    session = unwrap(session)
    return (session.scalars(select(DocumentRecord).join(DocumentVersion, DocumentVersion.document_id == DocumentRecord.id).where(
                DocumentVersion.series_id == series.id).order_by(DocumentRecord.id).with_for_update(of=DocumentRecord, nowait=True)
                .execution_options(populate_existing=True)).all())


def remove_document_bindings(row, session):
    session = unwrap(session)
    return (session.execute(delete(KnowledgeDocument).where(KnowledgeDocument.document_id == row.id)))


def count_active_pins(did, now, session):
    session = unwrap(session)
    return (session.scalar(select(func.count()).select_from(RunIndexPin).join(ChatRun, ChatRun.id == RunIndexPin.run_id).where(
                RunIndexPin.document_id == did, ChatRun.state.in_(("queued", "running", "waiting", "cancelling")),
                ChatRun.expires_at > now)))


def remove_hierarchy(did, session, uid):
    session = unwrap(session)
    return (session.execute(delete(HierarchyNode).where(HierarchyNode.document_id == did, HierarchyNode.user_id == uid)))
